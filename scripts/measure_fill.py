"""Measure per-column fill rates from a real sample and write src/homeharvest_mcp/fill_rates.json.

    python scripts/measure_fill.py            # Phoenix, AZ sample (~25 requests)
    python scripts/measure_fill.py "Austin, TX"

Fill % = share of rows where the value is not NULL (and not an empty string/list).
"""

import json
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

from homeharvest_mcp import schema
from homeharvest_mcp.engine import Engine, Settings
from homeharvest_mcp.store import Store

loc = sys.argv[1] if len(sys.argv) > 1 else "Phoenix, AZ"
since = (date.today() - timedelta(days=20)).isoformat()
tmp = Path(tempfile.mkdtemp()) / "fill.duckdb"
engine = Engine(store=Store(tmp))
settings = Settings.from_env(max_pages=4, max_history=300)

queries = [
    f"SELECT count(*) FROM properties WHERE location = '{loc}' AND status = 'for_sale'",
    f"SELECT count(*) FROM properties WHERE location = '{loc}' AND status = 'pending'",
    f"SELECT count(*) FROM properties WHERE location = '{loc}' AND status = 'sold' AND sold_date >= '{since}'",
    f"SELECT count(*) FROM properties WHERE location = '{loc}' AND status = 'for_rent'",
]
for q in queries:
    print(engine.query(q, settings=settings).splitlines()[-1], file=sys.stderr)

# history for a 300-home sample across statuses
hist_q = (f"SELECT count(*) FROM price_history h JOIN properties p USING (property_id) WHERE location = '{loc}' "
          f"AND status IN ('for_sale', 'pending', 'sold', 'for_rent') AND (sold_date >= '{since}' OR status <> 'sold')")
print(engine.query(hist_q, settings=settings).splitlines()[-1], file=sys.stderr)

out = {"tables": {}, "rows": {}}
with engine.store.connect() as con:
    sl = [r[0] for r in con.execute("SELECT slice_id FROM t_slices").fetchall()]
    enriched = [r[0] for r in con.execute("SELECT property_id FROM t_enriched").fetchall()]
    engine._views(con, sl, enriched, scoped_history=True)
    for t, meta in schema.TABLES.items():
        n = con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        out["rows"][t] = n
        fills = {}
        for name, typ, _ in meta["columns"]:
            if typ.endswith("[]"):
                cond = f"{name} IS NOT NULL AND len({name}) > 0"
            elif typ == "VARCHAR":
                cond = f"{name} IS NOT NULL AND {name} <> ''"
            else:
                cond = f"{name} IS NOT NULL"
            k = con.execute(f"SELECT count(*) FILTER (WHERE {cond}) FROM {t}").fetchone()[0]
            fills[name] = round(100.0 * k / n, 1) if n else None
        out["tables"][t] = fills
    by_status = dict(con.execute("SELECT status, count(*) FROM properties GROUP BY 1").fetchall())
    homes = con.execute("SELECT count(DISTINCT property_id) FROM properties WHERE property_id IN (SELECT property_id FROM t_enriched)").fetchone()[0]

parts = ", ".join(f"{v} {k}" for k, v in sorted(by_status.items()))
out["sample"] = (f"{loc}, {date.today().isoformat()}: {sum(by_status.values())} properties ({parts}); "
                 f"history tables from {homes} homes")
dest = Path(__file__).resolve().parents[1] / "src" / "homeharvest_mcp" / "fill_rates.json"
dest.write_text(json.dumps(out, indent=1) + "\n")
print(out["sample"])
print(json.dumps(out["rows"]))
