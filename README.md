# HomeHarvest MCP: US property data via SQL

An MCP server that lets AI agents query US real estate listings (for sale, pending, sold, rentals) with SQL. It runs on [HomeHarvest](https://github.com/ZacharyHampton/HomeHarvest), which reads Realtor.com.

The agent writes a `SELECT`. The server fetches only the listings that query needs, caches them in a local DuckDB file, runs the SQL there, and returns a few compact NDJSON rows. An aggregate like "median sold price by ZIP" comes back as 10 small rows, not 2,000 raw listings.

```
agent ──SQL──▶ parse (sqlglot) ──▶ pushdown filters ──▶ HomeHarvest (200 rows/page, only missing slices)
                    │                                          │
                    └──────────── DuckDB cache (24h TTL) ◀─────┘
                                      │
                         run full SQL locally ──▶ NDJSON rows + 1 metadata line
```

## Tools

| Tool | What it does |
|---|---|
| `describe(table?)` | Tables, columns, types, measured fill %, which filters are sent to the source and what they cost, example queries. |
| `query(sql, max_rows=50)` | One read-only `SELECT` (DuckDB dialect). Returns NDJSON rows, then `{"truncated","total_rows","fetched_pages","cache":"hit\|miss\|partial","complete",...}`. |
| `health()` | Server and HomeHarvest versions, cache size, settings. |

## Example queries

```sql
-- Median sold price by ZIP since Oct 1 (10 rows back)
SELECT zip, median(sold_price) AS med, count(*) FROM properties
WHERE location = 'Phoenix, AZ' AND status = 'sold' AND sold_date >= '2026-10-01'
GROUP BY zip ORDER BY med DESC LIMIT 10

-- 3+ bed homes for sale in one ZIP
SELECT address, list_price, beds, baths, sqft, days_on_mls, url FROM properties
WHERE zip = '85004' AND status = 'for_sale' AND beds >= 3 ORDER BY list_price LIMIT 20

-- Agents with the most active listings, with email and phone
SELECT name, email, phone, count(*) AS listings FROM agents
WHERE location = 'Scottsdale, AZ' AND status = 'for_sale' GROUP BY ALL ORDER BY listings DESC LIMIT 20

-- Upcoming open houses
SELECT p.address, o.start_time, o.end_time FROM open_houses o JOIN properties p USING (property_id)
WHERE location = 'Tempe, AZ' AND status = 'for_sale' AND o.start_time >= current_date ORDER BY o.start_time

-- Price history of one home (no location needed)
SELECT date, event, price FROM price_history WHERE property_id = '1365850953' ORDER BY date

-- Comps: sold within half a mile of an address
SELECT address, sold_price, sold_date, sqft FROM properties
WHERE location = '407 E Honda Bow Rd, Phoenix, AZ 85086' AND radius = 0.5
  AND status = 'sold' AND sold_date >= '2026-06-01'
```

## Schema

| Table | Grain | Key | Notes |
|---|---|---|---|
| `properties` | 1 home per status | `property_id` | prices, list/pending/sold dates, beds, baths, sqft, lot, year built, type, address parts, lat/lon, days on MLS, HOA, price/sqft, estimated value, URL, photo, description, tags |
| `agents` | 1 agent per listing | `(agent_id, property_id)` | name, email, phones, office, broker, NRDS id |
| `offices` | 1 office per status | `(office_id, status)` | name, email, phones, listing count in scope |
| `open_houses` | 1 open house | `(property_id, start_time)` | start, end, time zone, method |
| `price_history` | 1 listing/sale event | `(property_id, date, event)` | fetched on first use, 50 homes per request |
| `tax_history` | 1 tax year | `(property_id, year)` | tax, assessed values; fetched on first use |

Hop between tables with `property_id`, `agent_id`, and `office_id`. Call `describe('properties')` for every column with its fill rate (measured on a real Phoenix sample; regenerate with `python scripts/measure_fill.py "City, ST"`).

### What is sent to the source (pushdown)

Every `WHERE` condition still runs locally, so pushdown can only make fetching cheaper, never change the answer. Only top-level `AND` conditions are pushed.

| Filter | Pushed as |
|---|---|
| `location = / IN` (or `zip`, or `city` + `state`) | **required** search location: city, ZIP, county, neighborhood, or street address |
| `radius = n` | comps search around a street address |
| `status = / IN` | one search per status (`for_sale`, `pending`, `sold`, `for_rent`) |
| `sold_date` (sold), `list_date` (for_sale/for_rent) | date window, fetched as calendar-month slices |
| `beds`, `sqft`, `lot_sqft`, `year_built` | min/max |
| `list_price` (for_sale/pending) | min/max |
| `property_type = / IN` | type filter |
| `is_foreclosure = true` | foreclosure-only search |

Local only: `baths`, `pending_date` (the source ignores it), `sold_price`, `hoa_fee`, tags, text, open-house times, anything under `OR`/`NOT`.

### Fetching and cache

- 1 request returns 200 rows. Each `query` call fetches at most `max_pages` pages (default 25). If it stops early, the metadata says `"complete": false` and a re-run continues where it stopped.
- `SELECT ... LIMIT n` with no `ORDER BY`/`GROUP BY` stops fetching as soon as `n` rows match.
- The source returns at most 10,000 rows per search. Dated windows above that are split automatically.
- Rows are cached by (location, status, month, pushed filters) for 24 hours. A repeated query costs zero requests (`"cache": "hit"`).
- Queries are `SELECT` only, limited to the six tables, and DuckDB file/network access is off.

## Install

Requires [uv](https://docs.astral.sh/uv/).

**Claude Code**

```bash
claude mcp add homeharvest -- uvx --from git+https://github.com/ZacharyHampton/homeharvest-mcp homeharvest-mcp
```

**Claude Desktop** (`claude_desktop_config.json`) and **Cursor** (`~/.cursor/mcp.json`)

```json
{
  "mcpServers": {
    "homeharvest": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/ZacharyHampton/homeharvest-mcp", "homeharvest-mcp"],
      "env": { "HOMEHARVEST_PROXY": "" }
    }
  }
}
```

**Smithery (hosted)**

```bash
npx -y @smithery/cli install @ZacharyHampton/homeharvest-mcp --client claude
```

**Streamable HTTP (self-hosted)**

```bash
uvx --from git+https://github.com/ZacharyHampton/homeharvest-mcp homeharvest-mcp --transport http --host 0.0.0.0 --port 8081
# endpoint: http://<host>:8081/mcp   config as query params: /mcp?proxy_url=http://user:pass@host:port&max_pages=10
```

## Configuration

| Env var (stdio) | Smithery / HTTP param | Default | Meaning |
|---|---|---|---|
| `HOMEHARVEST_PROXY` | `proxy_url` | none | Proxy for Realtor.com requests (use one if you see 403s) |
| `HOMEHARVEST_MCP_MAX_PAGES` | `max_pages` | 25 | Pages (200 rows each) fetched per query call |
| `HOMEHARVEST_MCP_TTL_HOURS` | `cache_ttl_hours` | 24 | How long cached rows are reused |
| `HOMEHARVEST_MCP_MAX_HISTORY` | | 500 | Homes per call for price/tax history |
| `HOMEHARVEST_MCP_CACHE_DIR` | | `~/.cache/homeharvest-mcp` | Where `cache.duckdb` lives (several clients can share it) |

## Development

```bash
uv sync --extra test
uv run pytest            # offline unit tests
uv run pytest -m live -s # live smoke test against Realtor.com
uv run dev               # Smithery dev server
```

## Changes from v1

v1 had one tool, `scrape_properties`, that returned raw listing JSON: about 15K to 21K tokens for 5 listings. It pinned HomeHarvest 0.6.1, and its proxy setting never reached the scraper (the Smithery config schema wasn't registered). v2 replaces it with the SQL interface above. A 5-row, 6-column answer is about 350 tokens, and an aggregate over 2,000+ sold homes is about 150.

---

For real estate agents: Modern Realty writes your lead texts. https://www.modernrealty.io/?utm_source=mcp&utm_medium=homeharvest
