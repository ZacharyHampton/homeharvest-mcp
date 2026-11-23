"""Query engine: plan -> fetch missing slices -> run SQL in DuckDB -> NDJSON."""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

import duckdb

from . import schema
from .source import MAX_OFFSET, PAGE_SIZE, Location, SliceSpec, Source, SourceError, flatten, flatten_history
from .sqlplan import Plan, QueryError, plan_query
from .store import Store, fresh_cutoff, slice_id

MAX_ROWS_HARD = 1000
CELL_CHARS = 1000
OUTPUT_CHARS = 120_000  # ~30K tokens: hard ceiling for one response


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass
class Settings:
    proxy: str | None = None
    max_pages: int = 25
    ttl_hours: float = 24.0
    max_history: int = 500  # homes per call for price/tax history (50 per request)

    @classmethod
    def from_env(cls, **overrides) -> "Settings":
        s = cls(
            proxy=os.environ.get("HOMEHARVEST_PROXY") or None,
            max_pages=int(_env_float("HOMEHARVEST_MCP_MAX_PAGES", 25)),
            ttl_hours=_env_float("HOMEHARVEST_MCP_TTL_HOURS", 24.0),
            max_history=int(_env_float("HOMEHARVEST_MCP_MAX_HISTORY", 500)),
        )
        for k, v in overrides.items():
            if v not in (None, ""):
                setattr(s, k, v)
        return s


@dataclass
class FetchStats:
    pages: int = 0
    history_requests: int = 0
    slices_hit: int = 0
    slices_fetched: int = 0
    complete: bool = True
    source_total: int = 0
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    scope: list[str] = field(default_factory=list)  # slice ids gathered so far
    stop_check: object = None  # callable(slice_ids) -> bool: LIMIT already satisfied locally
    early_stop: bool = False

    def satisfied(self, sid: str) -> bool:
        if self.stop_check is not None and self.stop_check(self.scope + [sid]):
            self.early_stop = True
            return True
        return False


class Engine:
    def __init__(self, store: Store | None = None, source_factory=Source):
        self.store = store or Store()
        self.source_factory = source_factory

    # ------------------------------------------------------------------ fetch
    def _resolve(self, src: Source, spec: SliceSpec, settings: Settings, stats: FetchStats) -> Location:
        key = spec.location_key
        with self.store.connect() as con:
            row = self.store.get_location(con, key, datetime.now() - timedelta(days=30))
        if row:
            text, area_type, lat, lon, mpr_id = row
            return Location(text=text, area_type=area_type, centroid={"lat": lat, "lon": lon} if lat is not None else None, mpr_id=mpr_id)
        loc = src.resolve(spec)
        with self.store.connect() as con:
            self.store.put_location(con, key, loc)
        return loc

    def _ensure(self, src: Source, spec: SliceSpec, settings: Settings, stats: FetchStats, budget: list[int]) -> list[str]:
        """Make sure a slice is cached (fetching missing pages within budget). Returns slice ids in scope."""
        sid = slice_id(spec.key)
        cutoff = fresh_cutoff(settings.ttl_hours)
        with self.store.connect() as con:
            sl = self.store.get_slice(con, sid)
            if sl and sl["fetched_at"] < cutoff:
                sl = None
            # An unfiltered complete slice can answer a filtered query (filters re-run locally),
            # except foreclosure: unfiltered rows don't know whether they're foreclosures.
            if sl is None and spec.filter_sig and "foreclosure" not in spec.filter_dict:
                alt = self.store.find_unfiltered(con, spec.location_key, spec.status, spec.bucket, cutoff)
                if alt and not alt["children"]:
                    stats.slices_hit += 1
                    stats.source_total += alt["source_total"] or 0
                    return [alt["slice_id"]] + json.loads(alt["children"] or "[]")
            if sl is None:
                self.store.reset_slice(con, sid, spec, datetime.now())
                sl = self.store.get_slice(con, sid)

        children = json.loads(sl["children"] or "[]")
        if children:
            ids = [sid]
            for child in self._children_specs(spec, len(children)):
                ids += self._ensure(src, child, settings, stats, budget)
            return ids
        if sl["complete"]:
            stats.slices_hit += 1
            stats.source_total += sl["source_total"] or 0
            if sl["capped_reason"]:
                stats.complete = False
                stats.notes.append(sl["capped_reason"])
            return [sid]

        if stats.satisfied(sid):
            return [sid]
        if budget[0] <= 0:
            stats.complete = False
            if sl["rows"]:
                stats.source_total += sl["source_total"] or 0
            return [sid]

        stats.slices_fetched += 1
        loc = self._resolve(src, spec, settings, stats)
        fetched_at = datetime.now()

        if spec.status == "any" or (loc.area_type == "address" and not spec.radius):
            if not loc.mpr_id:
                raise SourceError(f"'{spec.location}' looks like an address but the source has no home record for it.")
            homes = src.fetch_home(spec, loc)
            budget[0] -= 1
            stats.pages += 1
            self._store_page(sid, homes, spec, fetched_at)
            with self.store.connect() as con:
                self.store.update_slice(con, sid, next_offset=len(homes), source_total=len(homes), pages=1, rows=len(homes), complete=True)
            stats.source_total += len(homes)
            return [sid]
        if spec.radius and loc.area_type != "address":
            raise SourceError(f"radius only works around a street address; '{spec.location}' resolved to a {loc.area_type}.")

        offset = sl["next_offset"] or 0
        pages, rows, total = sl["pages"] or 0, sl["rows"] or 0, sl["source_total"]
        complete, capped = False, None
        while budget[0] > 0:
            if pages and stats.satisfied(sid):
                break
            if offset >= MAX_OFFSET:
                complete = True
                capped = (
                    f"{spec.location} {spec.status}: the source stops at 10,000 rows per search "
                    f"({total} exist). Add a date filter or a smaller location (zip) to see the rest."
                )
                break
            try:
                page = src.fetch_page(spec, loc, offset)
            except SourceError as e:
                stats.errors.append(str(e))
                stats.complete = False
                break
            budget[0] -= 1
            pages += 1
            stats.pages += 1
            if page.total is not None:
                total = page.total
            # Split a dated window that holds more than the source will page through.
            if (
                offset == 0 and total and total > MAX_OFFSET and spec.date_from and spec.date_to
                and (spec.date_to - spec.date_from).days >= 1
            ):
                n = min(math.ceil(total / (MAX_OFFSET * 0.8)), (spec.date_to - spec.date_from).days + 1)
                kids = self._children_specs(spec, n)
                self._store_page(sid, page.homes, spec, fetched_at)
                with self.store.connect() as con:
                    self.store.update_slice(
                        con, sid, next_offset=PAGE_SIZE, source_total=total, pages=pages,
                        rows=rows + len(page.homes), complete=True, children=json.dumps([k.key for k in kids]),
                    )
                ids = [sid]
                for k in kids:
                    ids += self._ensure(src, k, settings, stats, budget)
                return ids
            self._store_page(sid, page.homes, spec, fetched_at)
            rows += len(page.homes)
            offset += PAGE_SIZE
            done = page.raw_count < PAGE_SIZE or (total is not None and offset >= total)
            with self.store.connect() as con:
                self.store.update_slice(con, sid, next_offset=offset, source_total=total, pages=pages, rows=rows, complete=done)
            if done:
                complete = True
                break
        if capped:
            with self.store.connect() as con:
                self.store.update_slice(con, sid, complete=True, capped_reason=capped)
            stats.notes.append(capped)
            stats.complete = False
        elif not complete and not stats.early_stop:
            stats.complete = False
        stats.source_total += total or rows
        return [sid]

    @staticmethod
    def _children_specs(spec: SliceSpec, n: int) -> list[SliceSpec]:
        days = (spec.date_to - spec.date_from).days + 1
        step = max(1, math.ceil(days / n))
        out, a = [], spec.date_from
        while a <= spec.date_to:
            b = min(spec.date_to, a + timedelta(days=step - 1))
            out.append(SliceSpec(spec.location, spec.status, a, b, spec.filters, spec.radius))
            a = b + timedelta(days=1)
        return out

    def _store_page(self, sid: str, homes: list[dict], spec: SliceSpec, fetched_at: datetime):
        tables: dict[str, list] = {"properties": [], "agents": [], "offices": [], "open_houses": []}
        foreclosure = bool(spec.filter_dict.get("foreclosure"))
        for h in homes:
            if not isinstance(h, dict) or not h.get("property_id"):
                continue
            t = flatten(h, spec.status, fetched_at, foreclosure=foreclosure)
            st = t["properties"][0]["status"]
            for a in t["agents"] + t["offices"]:
                a["status"] = st
            for o in t["open_houses"]:
                o["status"] = st
            for k in tables:
                tables[k] += t[k]
        with self.store.connect() as con:
            for k, rows in tables.items():
                self.store.insert_rows(con, k, rows, sid)

    def _histories(self, src: Source, ids: list[str], settings: Settings, stats: FetchStats):
        with self.store.connect() as con:
            missing = self.store.missing_history(con, ids, fresh_cutoff(max(settings.ttl_hours, 24 * 7)))
        if not missing:
            return
        todo = missing[: settings.max_history]
        if len(missing) > len(todo):
            stats.complete = False
            stats.notes.append(
                f"price/tax history fetched for {len(todo)} of {len(missing)} homes (limit {settings.max_history} per call). "
                "Re-run to fetch more, or narrow the homes first."
            )
        try:
            got = src.fetch_histories(todo)
        except SourceError as e:
            stats.errors.append(str(e))
            stats.complete = False
            return
        stats.history_requests += math.ceil(len(todo) / 50)
        with self.store.connect() as con:
            self.store.put_history(con, got, flatten_history)

    def _scope_has_rows(self, sids: list[str]) -> bool:
        if not sids:
            return False
        with self.store.connect() as con:
            sl = ", ".join(f"'{s}'" for s in sids)
            return con.execute(f"SELECT count(*) FROM t_properties WHERE slice_id IN ({sl})").fetchone()[0] > 0

    def _count_at_least(self, plan: Plan, sids: list[str], n: int) -> bool:
        with self.store.connect() as con:
            self._views(con, sids, plan.history_ids, scoped_history=True)
            try:
                got = con.execute(f"SELECT count(*) FROM (SELECT 1 FROM ({plan.sql}) LIMIT {int(n)})").fetchone()[0]
            except duckdb.Error:
                return False
        return got >= n

    # ------------------------------------------------------------------ views
    @staticmethod
    def _views(con, slice_ids: list[str], history_ids: list[str], scoped_history: bool):
        sl = ", ".join(f"'{s}'" for s in slice_ids) or "NULL"
        pcols = []
        for name, _t, _ in schema.PROPERTIES:
            if name == "location":
                pcols.append("location_key AS location")
            elif name == "has_open_house":
                pcols.append(
                    f"property_id IN (SELECT property_id FROM t_open_houses WHERE slice_id IN ({sl})) AS has_open_house"
                )
            else:
                pcols.append(f'"{name}"')
        con.execute(
            f"""CREATE OR REPLACE TEMP VIEW properties AS
            SELECT {', '.join(pcols)} FROM (
                SELECT t.*, s.location_key,
                       row_number() OVER (PARTITION BY t.property_id, t.status, s.location_key ORDER BY t.fetched_at DESC) AS _rn
                FROM t_properties t JOIN t_slices s USING (slice_id)
                WHERE t.slice_id IN ({sl})
            ) WHERE _rn = 1"""
        )
        for t in ("agents", "open_houses"):
            cols = ", ".join(f'"{c}"' for c in schema.column_names(t))
            con.execute(f"CREATE OR REPLACE TEMP VIEW {t} AS SELECT DISTINCT {cols} FROM t_{t} WHERE slice_id IN ({sl})")
        con.execute(
            f"""CREATE OR REPLACE TEMP VIEW offices AS
            SELECT office_id, status, any_value(name) AS name, any_value(email) AS email, any_value(phone) AS phone,
                   any_value(phones) AS phones, any_value(mls_set) AS mls_set,
                   count(DISTINCT property_id) AS listings, list(DISTINCT property_id ORDER BY property_id) AS property_ids
            FROM t_offices WHERE slice_id IN ({sl}) GROUP BY office_id, status"""
        )
        ids = ", ".join("'" + i.replace("'", "''") + "'" for i in history_ids) or "NULL"
        scope = f"property_id IN ({ids})"
        if scoped_history:
            scope = f"({scope} OR property_id IN (SELECT property_id FROM t_properties WHERE slice_id IN ({sl})))"
        for t in ("price_history", "tax_history"):
            cols = ", ".join(f'"{c}"' for c in schema.column_names(t))
            con.execute(f"CREATE OR REPLACE TEMP VIEW {t} AS SELECT DISTINCT {cols} FROM t_{t} WHERE {scope}")

    # ------------------------------------------------------------------ query
    def query(self, sql: str, max_rows: int = 50, settings: Settings | None = None) -> str:
        t0 = time.monotonic()
        settings = settings or Settings.from_env()
        try:
            max_rows = max(1, min(int(max_rows), MAX_ROWS_HARD))
        except (TypeError, ValueError):
            max_rows = 50
        try:
            plan = plan_query(sql)
        except QueryError as e:
            return _error(str(e))

        stats = FetchStats()
        src = self.source_factory(proxy=settings.proxy)
        budget = [max(1, int(settings.max_pages))]
        slice_ids: list[str] = []
        try:
            if plan.limit_hint:
                stats.stop_check = lambda sids: self._count_at_least(plan, sids, plan.limit_hint)
            for spec in plan.specs:
                slice_ids += self._ensure(src, spec, settings, stats, budget)
                stats.scope = list(slice_ids)
        except SourceError as e:
            if not slice_ids:
                return _error(str(e))
            stats.errors.append(str(e))
            stats.complete = False

        if stats.errors and not self._scope_has_rows(slice_ids) and not plan.history_ids:
            return _error(stats.errors[0])

        if plan.needs_history:
            ids = list(plan.history_ids)
            if not ids and slice_ids:
                with self.store.connect() as con:
                    sl = ", ".join(f"'{s}'" for s in slice_ids)
                    ids = [r[0] for r in con.execute(
                        f"SELECT DISTINCT property_id FROM t_properties WHERE slice_id IN ({sl})"
                    ).fetchall()]
            self._histories(src, ids, settings, stats)

        try:
            with self.store.connect() as con:
                self._views(con, slice_ids, plan.history_ids, scoped_history=bool(plan.specs))
                cur = con.execute(plan.sql)
                cols = [d[0] for d in cur.description]
                rows = cur.fetchmany(max_rows + 1)
                truncated = len(rows) > max_rows
                rows = rows[:max_rows]
                total_rows = len(rows)
                if truncated:
                    total_rows = con.execute(f"SELECT count(*) FROM ({plan.sql})").fetchone()[0]
        except duckdb.Error as e:
            return _error(_teach_duckdb(e, sorted(plan.tables)))

        lines, size, cut_cells = [], 0, 0
        for r in rows:
            obj = {}
            for c, v in zip(cols, r):
                v, cut = _jsonable(v)
                cut_cells += cut
                obj[c] = v
            line = json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
            if size + len(line) > OUTPUT_CHARS:
                truncated = True
                stats.notes.append(f"Output capped at ~{OUTPUT_CHARS // 4000}K tokens; select fewer columns or use LIMIT.")
                break
            lines.append(line)
            size += len(line) + 1
        if cut_cells:
            stats.notes.append(f"{cut_cells} long text value(s) cut to {CELL_CHARS} chars.")

        if stats.pages == 0 and stats.history_requests == 0:
            cache = "hit"
        elif stats.slices_hit == 0:
            cache = "miss"
        else:
            cache = "partial"
        meta = {
            "truncated": truncated,
            "total_rows": total_rows,
            "fetched_pages": stats.pages + stats.history_requests,
            "cache": cache,
            "complete": stats.complete,
        }
        if plan.specs:
            meta["source_rows"] = stats.source_total
        if stats.early_stop:
            stats.notes.append("Stopped fetching once the LIMIT was met (no ORDER BY/GROUP BY, so any matching rows qualify).")
        if not stats.complete and stats.pages and budget[0] <= 0:
            stats.notes.append(
                f"Stopped at the page cap ({settings.max_pages} pages x {PAGE_SIZE} rows). Re-run the same query to "
                "fetch more (cached pages are kept), or narrow the location/dates."
            )
        notes = list(dict.fromkeys(plan.notes + stats.notes))
        if notes:
            meta["notes"] = notes
        if stats.errors:
            meta["errors"] = list(dict.fromkeys(stats.errors))
        meta["elapsed_s"] = round(time.monotonic() - t0, 2)
        lines.append(json.dumps(meta, separators=(",", ":")))
        return "\n".join(lines)

    def health(self, settings: Settings | None = None) -> dict:
        from importlib.metadata import PackageNotFoundError, version

        from . import __version__

        settings = settings or Settings.from_env()
        try:
            hh = version("homeharvest")
        except PackageNotFoundError:
            hh = None
        return {
            "status": "ok",
            "server_version": __version__,
            "homeharvest_version": hh,
            "duckdb_version": duckdb.__version__,
            "cache": self.store.stats(),
            "settings": {
                "max_pages_per_call": settings.max_pages,
                "rows_per_page": PAGE_SIZE,
                "cache_ttl_hours": settings.ttl_hours,
                "max_history_homes_per_call": settings.max_history,
                "proxy_configured": bool(settings.proxy),
            },
        }


def _error(msg: str) -> str:
    return json.dumps({"error": msg}, ensure_ascii=False)


def _teach_duckdb(e: Exception, tables=None) -> str:
    msg = str(e).strip()
    first = msg.splitlines()[0][:400]
    if "Referenced column" in msg or "not found in FROM clause" in msg:
        hint = "; ".join(f"{t}: {', '.join(schema.column_names(t))}" for t in (tables or []))
        return f"{first}. Columns -> {hint}" if hint else f"{first}. Call describe() for column names."
    if "Permission Error" in msg or "external access" in msg.lower():
        return "File and network access is disabled in queries. Query the tables only."
    if "Conversion Error" in msg and "date" in msg.lower():
        return f"{first}. Write dates as 'YYYY-MM-DD'."
    return f"SQL error: {first}"


def _jsonable(v):
    if v is None or isinstance(v, (bool, int)):
        return v, 0
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return None, 0
        return (int(v) if v.is_integer() and abs(v) < 1e15 else round(v, 4)), 0
    if isinstance(v, Decimal):
        return _jsonable(float(v))
    if isinstance(v, datetime):
        return v.isoformat(timespec="seconds"), 0
    if isinstance(v, date):
        return v.isoformat(), 0
    if isinstance(v, str):
        if len(v) > CELL_CHARS:
            return v[:CELL_CHARS] + "…", 1
        return v, 0
    if isinstance(v, (list, tuple)):
        out, cut = [], 0
        for x in v:
            y, c = _jsonable(x)
            out.append(y)
            cut += c
        return out, cut
    if isinstance(v, dict):
        out, cut = {}, 0
        for k, x in v.items():
            y, c = _jsonable(x)
            out[str(k)] = y
            cut += c
        return out, cut
    return str(v), 0
