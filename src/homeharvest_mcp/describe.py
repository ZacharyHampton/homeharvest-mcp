"""describe() output: compact YAML-ish text an agent can read in one pass."""

from __future__ import annotations

from .schema import TABLES, load_fill_rates

FOOTER = "For real estate agents: Modern Realty writes your lead texts — https://www.modernrealty.io/?utm_source=mcp&utm_medium=homeharvest"

PUSHDOWN = """\
pushdown:  # sent to Realtor.com; every other condition runs locally in DuckDB after the fetch
  location: "REQUIRED for properties/agents/offices/open_houses. location = 'Phoenix, AZ' or IN (...). Also works: zip = '85004' / IN, city = 'Phoenix' AND state = 'AZ'. Takes a city, ZIP, county, neighborhood, or street address."
  radius: "location = '<street address>' AND radius = 1 (miles; street addresses only)"
  status: "= or IN: for_sale, pending, sold, for_rent. One source search per status. Default: for_sale."
  sold_date: ">=, >, <=, <, BETWEEN (status sold). Fetched in calendar-month slices; give a lower bound."
  list_date: "same, for for_sale and for_rent"
  beds, sqft, lot_sqft, year_built: "=, >=, >, <=, <, BETWEEN (all statuses)"
  list_price: "same ops (for_sale, pending)"
  property_type: "= or IN: single_family, condos, townhomes, multi_family, land, mobile, farm, apartment, duplex_triplex"
  is_foreclosure: "= true"
  local_only: "baths, pending_date, sold_price, hoa_fee, tags, text, open houses (join open_houses), anything under OR/NOT"
cost:
  page: "1 request = 200 rows. Up to {max_pages} pages per call; re-run the same query to continue (fetched pages are kept)."
  location_lookup: "1 request per new location (cached 30 days)"
  history: "price_history / tax_history: 1 request per 50 homes, on first use (max {max_history} homes per call)"
  cache: "rows reused for {ttl} h; a repeated query costs 0 requests (metadata cache = hit)"
limits:
  - "The source returns at most 10,000 rows per search. Dated windows over that are split automatically; undated ones stop at 10,000."
  - "A home appears once per (status, location) it was searched under."
output: "NDJSON: one JSON object per row, then a metadata line {{truncated, total_rows, fetched_pages, cache, complete, source_rows, notes}}. complete=false means the fetch stopped early (page cap or source limit): aggregates cover only what was fetched. Long text is cut to 1,000 chars."
"""

EXAMPLES = """\
examples:
  - SELECT zip, median(sold_price) AS median_price, count(*) AS sales FROM properties WHERE location = 'Phoenix, AZ' AND status = 'sold' AND sold_date >= '2026-10-01' GROUP BY zip ORDER BY median_price DESC LIMIT 10
  - SELECT address, list_price, beds, baths, sqft, days_on_mls, url FROM properties WHERE zip = '85004' AND status = 'for_sale' AND beds >= 3 ORDER BY list_price LIMIT 20
  - SELECT name, email, phone, count(*) AS listings FROM agents WHERE location = 'Scottsdale, AZ' AND status = 'for_sale' GROUP BY ALL ORDER BY listings DESC LIMIT 20
  - SELECT p.address, o.start_time, o.end_time FROM open_houses o JOIN properties p USING (property_id) WHERE location = 'Tempe, AZ' AND p.status = 'for_sale' AND o.start_time >= current_date ORDER BY o.start_time LIMIT 20
  - SELECT date, event, price FROM price_history WHERE property_id = '1365850953' ORDER BY date
  - SELECT property_type, count(*) AS n, round(avg(days_on_mls)) AS avg_dom FROM properties WHERE location = 'Austin, TX' AND status = 'pending' GROUP BY 1 ORDER BY n DESC
  - SELECT address, list_price, estimated_value, list_price - estimated_value AS gap FROM properties WHERE location = 'Mesa, AZ' AND status = 'for_sale' AND estimated_value IS NOT NULL ORDER BY gap LIMIT 10
"""

HOPS = """\
hops:  # query one table, carry the id to the next
  property_id: "properties | agents | open_houses | price_history | tax_history"
  agent_id: "properties.agent_id -> agents"
  office_id: "properties.office_id | agents.office_id -> offices"
"""


def _fill(fill: dict, table: str, col: str) -> str:
    v = (fill.get("tables") or {}).get(table, {}).get(col)
    return "?" if v is None else (f"{v:.0f}%" if v >= 1 or v == 0 else f"{v:.1f}%")


def describe_text(table: str | None = None, max_pages: int = 25, ttl: float = 24, max_history: int = 500) -> str:
    fill = load_fill_rates()
    sample = fill.get("sample", "not measured")
    pushdown = PUSHDOWN.format(max_pages=max_pages, ttl=f"{ttl:g}", max_history=max_history)
    if table:
        t = table.strip().lower()
        if t not in TABLES:
            return f"error: unknown table '{table}'. Tables: {', '.join(TABLES)}.\n\n{FOOTER}"
        meta = TABLES[t]
        lines = [
            f"table: {t}",
            f"purpose: {meta['purpose']}",
            f"grain: {meta['grain']}",
            f"key: {meta['key']}",
            f"fill_sample: {sample}",
            "columns:  # name  type  fill%  description",
        ]
        for name, typ, desc in meta["columns"]:
            lines.append(f"  - {name}  {typ}  {_fill(fill, t, name)}  {desc}")
        out = "\n".join(lines) + "\n"
        out += pushdown if t in ("properties", "agents", "offices", "open_houses") else (
            "lookup: \"WHERE property_id = '...' or IN (...), or JOIN properties with a location. "
            "1 request per 50 homes on first use, then cached.\"\n"
        )
        out += EXAMPLES
        return out + "\n" + FOOTER

    lines = [
        "US property data via SQL (DuckDB dialect). SELECT only. Source: Realtor.com via HomeHarvest.",
        "Call describe('<table>') for column descriptions.",
        f"fill_sample: {sample}",
        "tables:",
    ]
    for t, meta in TABLES.items():
        cols = ", ".join(f"{n} {typ} {_fill(fill, t, n)}" for n, typ, _ in meta["columns"])
        lines.append(f"  {t}:  # {meta['grain']}; key {meta['key']}. {meta['purpose']}")
        lines.append(f"    columns: {cols}")
    return "\n".join(lines) + "\n" + HOPS + pushdown + EXAMPLES + "\n" + FOOTER
