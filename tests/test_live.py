"""Live smoke test against Realtor.com. Run with: pytest -m live -s"""

import json

import pytest

from homeharvest_mcp.engine import Engine, Settings
from homeharvest_mcp.store import Store

SMOKE_SQL = (
    "SELECT zip, median(sold_price) AS med, count(*) FROM properties "
    "WHERE location='Phoenix, AZ' AND status='sold' AND sold_date >= '2026-10-01' "
    "GROUP BY zip ORDER BY med DESC LIMIT 10"
)


@pytest.mark.live
def test_phoenix_sold_by_zip_then_cache_hit(tmp_path):
    engine = Engine(store=Store(tmp_path / "live.duckdb"))
    settings = Settings.from_env()

    first = engine.query(SMOKE_SQL, settings=settings)
    lines = first.splitlines()
    rows, meta = [json.loads(x) for x in lines[:-1]], json.loads(lines[-1])
    print("\n--- first run ---\n" + first)
    print(f"chars={len(first)} (~{len(first) // 4} tokens)")
    assert "error" not in meta, meta
    assert meta["cache"] == "miss" and meta["fetched_pages"] > 0
    assert 1 <= len(rows) <= 10
    assert all(r["zip"] and r["med"] > 0 and r["count_star()"] > 0 for r in rows)
    assert [r["med"] for r in rows] == sorted((r["med"] for r in rows), reverse=True)

    second = engine.query(SMOKE_SQL, settings=settings)
    print("--- second run ---\n" + second)
    meta2 = json.loads(second.splitlines()[-1])
    if meta["complete"]:
        assert meta2["cache"] == "hit" and meta2["fetched_pages"] == 0
        assert second.splitlines()[:-1] == lines[:-1]
    else:  # page cap hit on run 1: run 2 resumes, run 3 must be a pure hit once complete
        third = engine.query(SMOKE_SQL, settings=settings)
        assert json.loads(third.splitlines()[-1])["cache"] in ("hit", "partial")


@pytest.mark.live
def test_agents_and_history_live(tmp_path):
    engine = Engine(store=Store(tmp_path / "live.duckdb"))
    s = Settings.from_env(max_pages=2)
    out = engine.query(
        "SELECT name, email, phone, count(*) AS listings FROM agents WHERE location = 'Tempe, AZ' "
        "AND status = 'for_sale' GROUP BY ALL ORDER BY listings DESC LIMIT 5", settings=s)
    print("\n" + out)
    rows = [json.loads(x) for x in out.splitlines()[:-1]]
    assert rows and rows[0]["listings"] >= 1
    out = engine.query(
        "SELECT h.property_id, h.date, h.event, h.price FROM price_history h JOIN properties p USING (property_id) "
        "WHERE location = 'Tempe, AZ' AND p.status = 'for_sale' AND p.list_price > 0 ORDER BY h.date DESC LIMIT 5",
        settings=s)
    print(out)
    assert len(out.splitlines()) >= 2
