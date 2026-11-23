"""SQL -> fetch plan.

1. Parse with sqlglot (DuckDB dialect). Only one read-only SELECT is allowed and
   only our six tables (plus CTE names) may appear in FROM/JOIN.
2. Pull pushdown filters out of each SELECT's top-level AND conditions.
   Pushdown is always a superset: the full WHERE still runs locally in DuckDB,
   so fetching too much is only slow, never wrong. Anything under OR/NOT is
   left for DuckDB.
3. Strip the pseudo-filters (location, radius) and hand the SQL to DuckDB.
"""

from __future__ import annotations

import calendar
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

import duckdb
import sqlglot
from sqlglot import exp

from .schema import DATE_COLUMN_FOR_STATUS, HISTORY_TABLES, LISTING_TABLES, STATUSES, TABLES
from .source import SliceSpec, looks_like_address

EXAMPLE_LOCATION = "WHERE location = 'Phoenix, AZ'"

# property_type values the source accepts as a search filter and returns as-is.
PUSHABLE_TYPES = {
    "single_family", "condos", "townhomes", "multi_family", "land", "mobile", "farm",
    "apartment", "duplex_triplex",
}
# our column -> source filter name, and which statuses the source applies it to correctly
NUMERIC_PUSHDOWN = {
    "beds": ("beds", set(STATUSES)),
    "sqft": ("sqft", set(STATUSES)),
    "lot_sqft": ("lot_sqft", set(STATUSES)),
    "year_built": ("year_built", set(STATUSES)),
    "list_price": ("price", {"for_sale", "pending"}),
}
DATE_COLUMNS = {"sold_date", "list_date", "pending_date"}
FORBIDDEN_NODES = tuple(
    getattr(exp, n) for n in (
        "Insert", "Update", "Delete", "Create", "Drop", "Alter", "Command", "Merge", "Copy",
        "Pragma", "Set", "Use", "Attach", "Detach", "Export", "Install", "LoadData", "Transaction",
        "Commit", "Rollback", "Describe",
    ) if hasattr(exp, n)
)


class QueryError(Exception):
    """Bad query; the message tells the agent how to fix it."""


@dataclass
class Plan:
    sql: str
    tables: set[str]
    specs: list[SliceSpec] = field(default_factory=list)
    history_ids: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    limit_hint: int | None = None  # set when any N matching rows answer the query

    @property
    def needs_listings(self) -> bool:
        return bool(self.tables & LISTING_TABLES)

    @property
    def needs_history(self) -> bool:
        return bool(self.tables & HISTORY_TABLES)


# ---------------------------------------------------------------------------
# constant folding via DuckDB (handles CURRENT_DATE - INTERVAL 30 DAY, casts...)
# ---------------------------------------------------------------------------
_const_con = None


def _const(node: exp.Expression):
    global _const_con
    if node.find(exp.Column, exp.Select, exp.Subquery):
        raise ValueError("not constant")
    if isinstance(node, exp.Literal):
        return node.to_py()
    if isinstance(node, exp.Boolean):
        return node.this
    if _const_con is None:
        _const_con = duckdb.connect(":memory:", config={"enable_external_access": False})
    return _const_con.execute(f"SELECT {node.sql(dialect='duckdb')}").fetchone()[0]


def _to_date(v) -> date:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        try:
            return date.fromisoformat(v.strip()[:10])
        except ValueError:
            pass
    raise QueryError(f"Can't read {v!r} as a date. Use 'YYYY-MM-DD', e.g. sold_date >= '2026-10-01'.")


def _strings(nodes) -> list[str]:
    out = []
    for n in nodes:
        v = _const(n)
        if not isinstance(v, str):
            raise ValueError("not a string")
        out.append(v)
    return out


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------

def _parse(sql: str) -> exp.Expression:
    if not sql or not sql.strip():
        raise QueryError("Empty query. Example: SELECT count(*) FROM properties " + EXAMPLE_LOCATION)
    try:
        stmts = [s for s in sqlglot.parse(sql, read="duckdb") if s is not None]
    except sqlglot.errors.ParseError as e:
        raise QueryError(f"SQL syntax error: {str(e).splitlines()[0][:300]}") from e
    if len(stmts) != 1:
        raise QueryError("Send one SELECT statement per call (no ';'-separated batches).")
    tree = stmts[0]
    if not isinstance(tree, exp.Query) or tree.find(*FORBIDDEN_NODES):
        raise QueryError("Read-only: only SELECT (WITH ... SELECT, UNION) is allowed. Use describe() for schemas.")
    return tree


def _check_tables(tree: exp.Expression) -> set[str]:
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    used: set[str] = set()
    for t in tree.find_all(exp.Table):
        if not isinstance(t.this, exp.Identifier):
            raise QueryError(
                f"Table functions aren't allowed ({t.this.sql(dialect='duckdb')[:60]}). "
                f"Query the tables: {', '.join(TABLES)}."
            )
        name = t.name.lower()
        if t.args.get("db") or t.args.get("catalog"):
            raise QueryError(f"Use bare table names ({', '.join(TABLES)}), not '{t.sql()}'.")
        if name in ctes:
            continue
        if name not in TABLES:
            raise QueryError(f"Unknown table '{t.name}'. Tables: {', '.join(TABLES)}. Call describe() for columns.")
        used.add(name)
    if not used:
        raise QueryError(f"Query at least one table: {', '.join(TABLES)}.")
    return used


# ---------------------------------------------------------------------------
# pushdown extraction
# ---------------------------------------------------------------------------

def _conjuncts(where: exp.Expression | None) -> list[exp.Expression]:
    if where is None:
        return []
    node = where.this if isinstance(where, exp.Where) else where
    out = []

    def walk(n):
        n = n.unnest() if isinstance(n, exp.Paren) else n
        if isinstance(n, exp.And):
            walk(n.left)
            walk(n.right)
        else:
            out.append(n)

    walk(node)
    return out


@dataclass
class _Filters:
    locations: list[str] = field(default_factory=list)
    cities: list[str] = field(default_factory=list)
    states: list[str] = field(default_factory=list)
    zips: list[str] = field(default_factory=list)
    radius: float | None = None
    statuses: list[str] | None = None
    dates: dict = field(default_factory=dict)  # col -> [lo, hi]
    nums: dict = field(default_factory=dict)  # col -> [min, max]
    property_types: list[str] | None = None
    foreclosure: bool = False
    property_ids: list[str] = field(default_factory=list)
    strip: list[exp.Expression] = field(default_factory=list)

    @property
    def has_location(self) -> bool:
        return bool(self.locations or self.cities or self.states or self.zips)


_FLIP = {exp.GT: exp.LT, exp.GTE: exp.LTE, exp.LT: exp.GT, exp.LTE: exp.GTE, exp.EQ: exp.EQ}


def _col_cmp(c: exp.Expression):
    """Normalize `col OP const` / `const OP col` -> (colname, op_class, const_node)."""
    if type(c) in _FLIP:
        left, right = c.left, c.right
        if isinstance(left, exp.Column) and not right.find(exp.Column):
            return left.name.lower(), type(c), right
        if isinstance(right, exp.Column) and not left.find(exp.Column):
            return right.name.lower(), _FLIP[type(c)], left
    return None


def _bound(f: dict, col: str, lo=None, hi=None):
    cur = f.setdefault(col, [None, None])
    if lo is not None:
        cur[0] = lo if cur[0] is None else max(cur[0], lo)
    if hi is not None:
        cur[1] = hi if cur[1] is None else min(cur[1], hi)


def _num_bounds(op, v: float):
    if op is exp.GTE:
        return math.ceil(v), None
    if op is exp.GT:
        return math.floor(v) + 1, None
    if op is exp.LTE:
        return None, math.floor(v)
    if op is exp.LT:
        return None, math.ceil(v) - 1
    if op is exp.EQ:
        return math.ceil(v), math.floor(v)
    return None, None


def _analyze(select: exp.Select) -> _Filters:
    f = _Filters()
    for c in _conjuncts(select.args.get("where")):
        try:
            _analyze_one(c, f)
        except ValueError:
            continue  # non-constant or odd shape: leave it for DuckDB
    return f


def _analyze_one(c: exp.Expression, f: _Filters):
    # bare boolean column: WHERE is_foreclosure
    if isinstance(c, exp.Column) and c.name.lower() == "is_foreclosure":
        f.foreclosure = True
        return
    if isinstance(c, exp.Is) and isinstance(c.this, exp.Column) and c.this.name.lower() == "is_foreclosure":
        if isinstance(c.expression, exp.Boolean) and c.expression.this:
            f.foreclosure = True
        return
    if isinstance(c, exp.In) and isinstance(c.this, exp.Column) and c.args.get("expressions"):
        col = c.this.name.lower()
        vals = c.args["expressions"]
        if col == "location":
            f.locations += _strings(vals)
            f.strip.append(c)
        elif col in ("city", "state", "zip"):
            {"city": f.cities, "state": f.states, "zip": f.zips}[col].extend(_strings(vals))
        elif col == "status":
            f.statuses = [s.lower() for s in _strings(vals)]
        elif col == "property_type":
            f.property_types = [s.lower() for s in _strings(vals)]
        elif col == "property_id":
            f.property_ids += [str(_const(v)) for v in vals]
        return
    if isinstance(c, exp.Between) and isinstance(c.this, exp.Column):
        col = c.this.name.lower()
        lo, hi = _const(c.args["low"]), _const(c.args["high"])
        if col in DATE_COLUMNS:
            _bound(f.dates, col, _to_date(lo), _to_date(hi))
        elif col in NUMERIC_PUSHDOWN:
            _bound(f.nums, col, math.ceil(float(lo)), math.floor(float(hi)))
        elif col == "radius":
            raise QueryError("Use radius = <miles> (e.g. radius = 1), not BETWEEN.")
        return
    cmp = _col_cmp(c)
    if not cmp:
        return
    col, op, node = cmp
    if col == "location":
        if op is not exp.EQ:
            raise QueryError("location only supports = or IN, e.g. " + EXAMPLE_LOCATION)
        f.locations += _strings([node])
        f.strip.append(c)
    elif col == "radius":
        if op not in (exp.EQ, exp.LTE, exp.LT):
            raise QueryError("Use radius = <miles>, e.g. WHERE location = '123 Main St, Phoenix, AZ 85004' AND radius = 1")
        f.radius = float(_const(node))
        f.strip.append(c)
    elif col in ("city", "state", "zip") and op is exp.EQ:
        {"city": f.cities, "state": f.states, "zip": f.zips}[col].extend(_strings([node]))
    elif col == "status" and op is exp.EQ:
        f.statuses = [s.lower() for s in _strings([node])]
    elif col == "property_type" and op is exp.EQ:
        f.property_types = [s.lower() for s in _strings([node])]
    elif col == "property_id" and op is exp.EQ:
        f.property_ids.append(str(_const(node)))
    elif col == "is_foreclosure" and op is exp.EQ:
        if _const(node) is True:
            f.foreclosure = True
    elif col in DATE_COLUMNS:
        d = _to_date(_const(node))
        if op in (exp.GT, exp.GTE):
            _bound(f.dates, col, lo=d)
        elif op in (exp.LT, exp.LTE):
            _bound(f.dates, col, hi=d)
        else:
            _bound(f.dates, col, d, d)
    elif col in NUMERIC_PUSHDOWN:
        v = _const(node)
        if isinstance(v, Decimal):
            v = float(v)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return
        lo, hi = _num_bounds(op, float(v))
        if lo is not None and hi is not None and lo > hi:
            return
        _bound(f.nums, col, lo, hi)


def _check_pseudo_columns(tree: exp.Expression, stripped: set[int]):
    """location/radius filters must be top-level AND conditions we understood."""
    for where in tree.find_all(exp.Where):
        for col in where.find_all(exp.Column):
            name = col.name.lower()
            if name not in ("location", "radius"):
                continue
            node, ok = col, False
            while node is not None and node is not where:
                if id(node) in stripped:
                    ok = True
                    break
                node = node.parent
            if not ok:
                if name == "radius":
                    raise QueryError("radius must be a top-level AND condition: WHERE location = '<street address>' AND radius = 1")
                raise QueryError(
                    "location must be a top-level AND condition with = or IN, e.g. "
                    "WHERE location IN ('Phoenix, AZ', 'Tempe, AZ') AND status = 'sold'. "
                    "(It can't sit inside OR/NOT.)"
                )
    for col in tree.find_all(exp.Column):
        if col.name.lower() == "radius" and not col.find_ancestor(exp.Where):
            raise QueryError("radius is a search setting, not a column. Use it only in WHERE.")


# ---------------------------------------------------------------------------
# date windows
# ---------------------------------------------------------------------------

def month_windows(lo: date, hi: date) -> list[tuple[date, date]]:
    """Calendar months covering [lo, hi], newest first. Whole months are fetched so
    later queries with other start days reuse the cache."""
    out = []
    y, m = hi.year, hi.month
    while (y, m) >= (lo.year, lo.month):
        start = date(y, m, 1)
        end = date(y, m, calendar.monthrange(y, m)[1])
        out.append((start, end))
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    return out


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------

def _locations_for(f: _Filters, notes: list[str]) -> list[str]:
    if f.locations:
        return list(dict.fromkeys(f.locations))
    if f.zips:
        return list(dict.fromkeys(f.zips))
    if f.cities:
        if f.states:
            return list(dict.fromkeys(f"{c}, {s}" for c in f.cities for s in f.states))
        notes.append("city without state: searched by city name only; add AND state = 'XX' to avoid a same-name city.")
        return list(dict.fromkeys(f.cities))
    if f.states:
        notes.append("State-wide search: the source returns at most 10,000 rows per search. Narrow with a city/zip/county or a date filter.")
        return list(dict.fromkeys(f.states))
    return []


def _specs_for(f: _Filters, today: date, notes: list[str]) -> list[SliceSpec]:
    locations = _locations_for(f, notes)
    statuses = f.statuses
    if statuses is not None:
        bad = [s for s in statuses if s not in STATUSES]
        if bad:
            raise QueryError(f"Unknown status {bad}. Use one of: {', '.join(STATUSES)}.")
    else:
        inferred = [s for s in ("sold", "pending") if DATE_COLUMN_FOR_STATUS[s] in f.dates]
        if "list_date" in f.dates or not inferred:
            inferred.append("for_sale")
        statuses = inferred
        if f.radius is not None or not all(looks_like_address(loc) for loc in locations):
                notes.append(
                f"No status filter: fetched {', '.join(statuses)}. Add WHERE status IN "
                "('for_sale','pending','sold','for_rent') to choose."
            )

    if f.radius is not None:
        bad = [loc for loc in locations if not looks_like_address(loc)]
        if bad:
            raise QueryError(
                f"radius only works around a street address; '{bad[0]}' is an area. "
                "Drop radius, or use WHERE location = '123 Main St, Phoenix, AZ 85004' AND radius = 1."
            )
        if not 0 < f.radius <= 50:
            raise QueryError("radius must be between 0 and 50 miles.")

    specs: list[SliceSpec] = []
    for loc in locations:
        if looks_like_address(loc) and f.radius is None:
            specs.append(SliceSpec(location=loc, status="any"))
            continue
        for st in statuses:
            filt: dict = {}
            for col, (name, ok_statuses) in NUMERIC_PUSHDOWN.items():
                if col in f.nums and st in ok_statuses:
                    lo, hi = f.nums[col]
                    if lo is not None:
                        filt[f"{name}_min"] = int(lo)
                    if hi is not None:
                        filt[f"{name}_max"] = int(hi)
            if f.property_types and all(t in PUSHABLE_TYPES for t in f.property_types):
                filt["property_type"] = ",".join(sorted(set(f.property_types)))
            if f.foreclosure:
                filt["foreclosure"] = True
            filters = tuple(sorted(filt.items()))

            dcol = DATE_COLUMN_FOR_STATUS[st]
            lo, hi = f.dates.get(dcol, [None, None])
            other_dates = [c for c in f.dates if c != dcol]
            if other_dates and len(statuses) == 1:
                notes.append(
                    f"{other_dates[0]} can't be sent to the source for status '{st}' (it filters on {dcol}); "
                    "it was applied locally after fetching."
                )
            if st == "pending" and (lo or hi):
                notes.append("pending_date can't be sent to the source (it ignores it); pending homes were fetched and filtered locally.")
                lo = hi = None
            if lo is None and hi is not None:
                notes.append(f"{dcol} has only an upper bound, so it was applied locally. Add {dcol} >= '...' to fetch less.")
                hi = None
            if lo is not None:
                hi = min(hi or today, today)
                if lo > hi:
                    continue
                # Radius searches are small: one window. Areas: calendar months (cache reuse, 10k cap).
                windows = [(lo, hi)] if f.radius is not None else month_windows(lo, hi)
                for a, b in windows:
                    specs.append(SliceSpec(location=loc, status=st, date_from=a, date_to=b, filters=filters, radius=f.radius))
            else:
                specs.append(SliceSpec(location=loc, status=st, filters=filters, radius=f.radius))
    return specs


def plan_query(sql: str, today: date | None = None) -> Plan:
    today = today or date.today()
    tree = _parse(sql)
    tables = _check_tables(tree)
    notes: list[str] = []

    filters = [_analyze(s) for s in tree.find_all(exp.Select)]
    stripped = {id(n) for f in filters for n in f.strip}
    _check_pseudo_columns(tree, stripped)

    plan = Plan(sql="", tables=tables, notes=notes)
    if plan.needs_listings or any(f.has_location for f in filters):
        seen = set()
        for f in filters:
            if not f.has_location:
                continue
            for spec in _specs_for(f, today, notes):
                if spec.key not in seen:
                    seen.add(spec.key)
                    plan.specs.append(spec)
        if not any(f.has_location for f in filters) and plan.needs_listings:
            raise QueryError(
                "Add a location so the server knows what to fetch: " + EXAMPLE_LOCATION +
                " (or WHERE zip = '85004', or WHERE city = 'Phoenix' AND state = 'AZ')."
            )
    plan.history_ids = list(dict.fromkeys(pid for f in filters for pid in f.property_ids))
    if plan.needs_history and not plan.specs and not plan.history_ids:
        raise QueryError(
            "price_history/tax_history need homes to look up: add WHERE property_id = '...' (or IN (...)), "
            "or join properties with a location, e.g. SELECT h.* FROM price_history h JOIN properties p "
            "USING (property_id) " + EXAMPLE_LOCATION + " AND status = 'sold' AND sold_date >= '2026-11-01'."
        )

    for node in [n for f in filters for n in f.strip]:
        node.replace(exp.true())
    plan.sql = tree.sql(dialect="duckdb") if stripped else sql.strip().rstrip(";")
    plan.notes = list(dict.fromkeys(notes))
    plan.limit_hint = _limit_hint(tree)
    return plan


def _limit_hint(tree: exp.Expression) -> int | None:
    """A plain `SELECT cols FROM one_table WHERE ... LIMIT n` is answered by any n matching
    rows, so fetching can stop as soon as n rows match locally."""
    if not isinstance(tree, exp.Select) or tree.args.get("with"):
        return None
    if any(tree.args.get(k) for k in ("group", "order", "having", "distinct", "joins", "qualify", "offset")):
        return None
    if tree.find(exp.AggFunc, exp.Window, exp.Subquery) or len(list(tree.find_all(exp.Select))) > 1:
        return None
    limit = tree.args.get("limit")
    if limit is None:
        return None
    try:
        n = int(_const(limit.expression))
    except (ValueError, TypeError, AttributeError):
        return None
    return n if 0 < n <= 1000 else None
