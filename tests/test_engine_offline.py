"""Engine + cache behavior against a fake source. No network."""

import json
from datetime import date, datetime, timedelta

import duckdb
import pytest

from homeharvest_mcp.engine import Engine, Settings
from homeharvest_mcp.source import Location, Page, SourceError
from homeharvest_mcp.store import Store


def home(pid, status="for_sale", zip_="85281", city="Tempe", price=400000, beds=3, pending=False,
         sold_date=None, agent="A1", office="O1", text="Nice home", open_house=False):
    return {
        "property_id": str(pid),
        "status": status,
        "list_price": price,
        "list_date": "2026-11-01T10:00:00Z",
        "last_sold_price": price if status == "sold" else None,
        "last_sold_date": sold_date,
        "flags": {"is_pending": pending or None},
        "description": {"beds": beds, "baths_full": 2, "baths_half": 1, "sqft": 1500, "type": "single_family", "text": text},
        "location": {"address": {"line": f"{pid} Main St", "city": city, "state_code": "AZ", "postal_code": zip_,
                                 "coordinate": {"lat": 33.4, "lon": -111.9}}, "county": {"name": "Maricopa"}},
        "advertisers": [{"type": "seller", "fulfillment_id": agent, "name": f"Agent {agent}", "email": f"{agent}@x.com",
                         "phones": [{"number": "(480) 555-0100", "type": "Mobile", "primary": True}],
                         "office": {"fulfillment_id": office, "name": f"Office {office}",
                                    "phones": [{"number": "4805550000", "type": "Office"}]}}],
        "open_houses": [{"start_date": "2026-11-29T18:00:00Z", "end_date": "2026-11-29T20:00:00Z"}] if open_house else None,
        "current_estimates": [{"estimate": price + 10000, "isBestHomeValue": True}],
        "href": f"https://example.com/{pid}",
    }


class FakeSource:
    instances = []

    def __init__(self, data, totals=None, fail=None):
        self.data = data  # (location_key, status) -> list[home]
        self.totals = totals or {}
        self.fail = fail
        self.calls = []
        self.history_calls = []
        self.proxy = None

    def __call__(self, proxy=None):
        self.proxy = proxy
        return self

    def resolve(self, spec):
        self.calls.append(("resolve", spec.location_key))
        return Location(text=spec.location, area_type="city")

    def fetch_page(self, spec, loc, offset):
        if self.fail:
            raise SourceError(self.fail)
        self.calls.append(("page", spec.key, offset))
        rows = self.data.get((spec.location_key, spec.status), [])
        if spec.date_from:
            rows = [r for r in rows if r.get("last_sold_date") is None
                    or spec.date_from.isoformat() <= r["last_sold_date"] <= spec.date_to.isoformat()]
        beds_min = spec.filter_dict.get("beds_min")
        if beds_min:
            rows = [r for r in rows if r["description"]["beds"] >= beds_min]
        total = self.totals.get((spec.location_key, spec.status), len(rows))
        return Page(total=total, homes=rows[offset:offset + 200], raw_count=len(rows[offset:offset + 200]))

    def fetch_home(self, spec, loc):
        return []

    def fetch_histories(self, ids):
        self.history_calls.append(list(ids))
        return {i: {"property_history": [{"date": "2026-11-01", "event_name": "Listed", "price": 1}],
                    "tax_history": [{"year": 2025, "tax": 100, "assessment": {"total": 5000}}]} for i in ids}

    def pages(self):
        return [c for c in self.calls if c[0] == "page"]


def run(engine, sql, **kw):
    s = Settings(max_pages=kw.pop("max_pages", 25), ttl_hours=kw.pop("ttl", 24), max_history=kw.pop("max_history", 500))
    out = engine.query(sql, settings=s, **kw).splitlines()
    rows = [json.loads(x) for x in out]
    return rows[:-1], rows[-1]


@pytest.fixture
def tempe():
    homes = [home(i, beds=2 + i % 4, pending=(i % 10 == 0), open_house=(i % 7 == 0)) for i in range(1, 451)]
    sold = [home(1000 + i, status="sold", sold_date="2026-11-%02d" % (1 + i % 20), price=300000 + i * 1000) for i in range(50)]
    sold += [home(2000 + i, status="sold", sold_date="2026-10-%02d" % (1 + i % 28)) for i in range(30)]
    return FakeSource({("tempe, az", "for_sale"): homes, ("tempe, az", "sold"): sold,
                       ("mesa, az", "for_sale"): [home(9000 + i, city="Mesa", zip_="85201") for i in range(10)]})


@pytest.fixture
def engine(tmp_path, tempe):
    return Engine(store=Store(tmp_path / "c.duckdb"), source_factory=tempe)


def test_miss_then_hit(engine, tempe):
    sql = "SELECT count(*) AS n FROM properties WHERE location = 'Tempe, AZ' AND status = 'for_sale'"
    rows, meta = run(engine, sql)
    assert meta["cache"] == "miss" and meta["fetched_pages"] == 3 and meta["complete"] is True
    assert set(meta) >= {"truncated", "total_rows", "fetched_pages", "cache"}
    n_first = rows[0]["n"]
    rows2, meta2 = run(engine, sql)
    assert meta2["cache"] == "hit" and meta2["fetched_pages"] == 0 and rows2[0]["n"] == n_first
    assert len(tempe.pages()) == 3
    assert [c for c in tempe.calls if c[0] == "resolve"] == [("resolve", "tempe, az")]


def test_pending_relabel(engine):
    rows, _ = run(engine, "SELECT status, count(*) n FROM properties WHERE location = 'Tempe, AZ' "
                          "AND status IN ('for_sale', 'pending') GROUP BY 1 ORDER BY 1")
    # pending ones come from the for_sale search relabeled (fake pending search is empty)
    assert {r["status"]: r["n"] for r in rows} == {"for_sale": 405, "pending": 45}


def test_page_cap_then_resume(engine, tempe):
    sql = "SELECT count(*) n FROM properties WHERE location = 'Tempe, AZ' AND status = 'for_sale'"
    rows, meta = run(engine, sql, max_pages=2)
    assert meta["complete"] is False and meta["fetched_pages"] == 2 and meta["source_rows"] == 450
    assert any("page cap" in n for n in meta["notes"])
    rows, meta = run(engine, sql, max_pages=2)
    assert meta["complete"] is True and meta["fetched_pages"] == 1
    assert [c[2] for c in tempe.pages()] == [0, 200, 400]


def test_filtered_query_reuses_unfiltered_slice(engine, tempe):
    run(engine, "SELECT count(*) FROM properties WHERE location = 'Tempe, AZ' AND status = 'for_sale'")
    rows, meta = run(engine, "SELECT count(*) n FROM properties WHERE location = 'Tempe, AZ' AND status = 'for_sale' AND beds >= 5")
    assert meta["cache"] == "hit"
    assert rows[0]["n"] == sum(1 for i in range(1, 451) if 2 + i % 4 >= 5 and i % 10 != 0)


def test_filtered_slice_does_not_serve_unfiltered(engine, tempe):
    run(engine, "SELECT count(*) FROM properties WHERE location = 'Tempe, AZ' AND status = 'for_sale' AND beds >= 5")
    rows, meta = run(engine, "SELECT count(*) n FROM properties WHERE location = 'Tempe, AZ' AND status = 'for_sale'")
    assert meta["cache"] == "miss"
    assert rows[0]["n"] == 405


def test_locations_do_not_leak(engine):
    run(engine, "SELECT count(*) FROM properties WHERE location = 'Tempe, AZ' AND status = 'for_sale'")
    rows, _ = run(engine, "SELECT count(*) n, min(city) c FROM properties WHERE location = 'Mesa, AZ' AND status = 'for_sale'")
    assert rows == [{"n": 10, "c": "Mesa"}]
    rows, _ = run(engine, "SELECT location, count(*) n FROM properties WHERE location IN ('Mesa, AZ', 'Tempe, AZ') "
                          "AND status = 'for_sale' GROUP BY 1 ORDER BY 1")
    assert rows == [{"location": "mesa, az", "n": 10}, {"location": "tempe, az", "n": 405}]


def test_sold_month_slices_and_aggregate(engine, tempe):
    rows, meta = run(engine, "SELECT count(*) n, median(sold_price) med FROM properties WHERE location = 'Tempe, AZ' "
                             "AND status = 'sold' AND sold_date >= '2026-10-01'")
    assert rows[0]["n"] == 80
    keys = sorted({c[1] for c in tempe.pages()})
    assert keys == ["tempe, az|sold|2026-10-01..2026-10-31|", "tempe, az|sold|2026-11-01..2026-11-30|"]
    # narrower later query: same month slices, no new fetch
    rows, meta = run(engine, "SELECT count(*) n FROM properties WHERE location = 'Tempe, AZ' AND status = 'sold' "
                             "AND sold_date >= '2026-11-10'")
    assert meta["cache"] == "hit"
    assert rows[0]["n"] == sum(1 for i in range(50) if 1 + i % 20 >= 10)


def test_ttl_expiry_refetches(engine, tempe):
    sql = "SELECT count(*) FROM properties WHERE location = 'Mesa, AZ' AND status = 'for_sale'"
    run(engine, sql)
    with engine.store.connect() as con:
        con.execute("UPDATE t_slices SET fetched_at = fetched_at - INTERVAL 2 DAY")
    _, meta = run(engine, sql)
    assert meta["cache"] == "miss"
    rows, _ = run(engine, "SELECT count(*) n FROM properties WHERE location = 'Mesa, AZ' AND status = 'for_sale'")
    assert rows[0]["n"] == 10  # old rows replaced, not duplicated


def test_truncation_and_total_rows(engine):
    rows, meta = run(engine, "SELECT property_id FROM properties WHERE location = 'Tempe, AZ' AND status = 'for_sale'", max_rows=5)
    assert len(rows) == 5 and meta["truncated"] is True and meta["total_rows"] == 405


def test_long_text_is_cut(tmp_path):
    src = FakeSource({("tempe, az", "for_sale"): [home(1, text="x" * 5000)]})
    e = Engine(store=Store(tmp_path / "c.duckdb"), source_factory=src)
    rows, meta = run(e, "SELECT description FROM properties WHERE location = 'Tempe, AZ' AND status = 'for_sale'")
    assert len(rows[0]["description"]) == 1001 and any("cut" in n for n in meta["notes"])


def test_child_tables(engine):
    rows, _ = run(engine, "SELECT name, email, phone, phones, count(*) n FROM agents WHERE location = 'Tempe, AZ' "
                          "AND status = 'for_sale' GROUP BY ALL")
    assert rows == [{"name": "Agent A1", "email": "A1@x.com", "phone": "4805550100", "phones": ["4805550100 (Mobile)"], "n": 405}]
    rows, _ = run(engine, "SELECT office_id, status, listings FROM offices WHERE location = 'Tempe, AZ' ORDER BY status")
    assert rows == [{"office_id": "O1", "status": "for_sale", "listings": 405}, {"office_id": "O1", "status": "pending", "listings": 45}]
    rows, _ = run(engine, "SELECT count(*) n FROM open_houses WHERE location = 'Tempe, AZ'")
    assert rows[0]["n"] == 450 // 7
    rows, _ = run(engine, "SELECT count(*) n FROM properties WHERE location = 'Tempe, AZ' AND has_open_house")
    assert rows[0]["n"] == 450 // 7


def test_history_fetched_once_and_scoped(engine, tempe):
    sql = ("SELECT count(*) n FROM price_history h JOIN properties p USING (property_id) "
           "WHERE location = 'Mesa, AZ' AND p.status = 'for_sale'")
    rows, meta = run(engine, sql)
    assert rows[0]["n"] == 10 and meta["fetched_pages"] == 2  # 1 listing page + 1 history request
    rows, meta = run(engine, sql)
    assert meta["cache"] == "hit" and len(tempe.history_calls) == 1
    rows, meta = run(engine, "SELECT year, tax, assessed_total FROM tax_history WHERE property_id = '9001'")
    assert rows == [{"year": 2025, "tax": 100, "assessed_total": 5000}] and meta["cache"] == "hit"


def test_history_cap(engine, tempe):
    rows, meta = run(engine, "SELECT count(DISTINCT property_id) n FROM price_history h JOIN properties p USING (property_id) "
                             "WHERE location = 'Tempe, AZ' AND p.status = 'for_sale'", max_history=100)
    assert len(tempe.history_calls[0]) == 100 and rows[0]["n"] <= 100 and meta["complete"] is False
    assert any("history" in n for n in meta["notes"])


def test_big_window_splits_under_10k(tmp_path):
    rows = [home(i, status="sold", sold_date="2026-11-05") for i in range(10)]
    src = FakeSource({("big, tx", "sold"): rows}, totals={("big, tx", "sold"): 25000})
    e = Engine(store=Store(tmp_path / "c.duckdb"), source_factory=src)
    _, meta = run(e, "SELECT count(*) FROM properties WHERE location = 'Big, TX' AND status = 'sold' AND sold_date >= '2026-11-01'")
    keys = [c[1] for c in src.pages()]
    assert keys[0] == "big, tx|sold|2026-11-01..2026-11-30|"
    assert len(set(keys[1:])) >= 4  # children windows
    assert all("..2026-11-30|" not in k or k.startswith("big, tx|sold|2026-11-2") for k in keys[1:])


def test_source_error_teaches(tmp_path):
    src = FakeSource({}, fail="Realtor.com blocked the request (403). Wait a minute and retry, or set a proxy")
    e = Engine(store=Store(tmp_path / "c.duckdb"), source_factory=src)
    out = e.query("SELECT count(*) FROM properties WHERE location = 'Tempe, AZ'", settings=Settings())
    # cold cache + failed fetch: an error, never a confident count of 0
    assert out.count("\n") == 0 and "proxy" in json.loads(out)["error"]


def test_query_errors_are_json(engine):
    out = json.loads(engine.query("SELECT nope FROM properties WHERE location = 'Tempe, AZ'", settings=Settings()))
    assert "property_id" in out["error"]  # lists the real columns
    out = json.loads(engine.query("SELECT * FROM properties", settings=Settings()))
    assert "location = 'Phoenix, AZ'" in out["error"]


def test_duckdb_blocks_files_even_if_planner_missed(tmp_path):
    store = Store(tmp_path / "c.duckdb")
    with store.connect() as con:
        with pytest.raises(duckdb.Error):
            con.execute("SELECT * FROM read_csv('/etc/hosts')").fetchall()


def test_proxy_reaches_source(engine, tempe):
    engine.query("SELECT 1 FROM properties WHERE location = 'Tempe, AZ'", settings=Settings(proxy="http://p:1"))
    assert tempe.proxy == "http://p:1"


def test_health(engine):
    h = engine.health(Settings(proxy="http://x"))
    assert h["status"] == "ok" and h["settings"]["proxy_configured"] is True and "rows" in h["cache"]


def test_limit_stops_fetching_early(engine, tempe):
    sql = "SELECT property_id FROM properties WHERE location = 'Tempe, AZ' AND status = 'for_sale' LIMIT 5"
    rows, meta = run(engine, sql)
    assert len(rows) == 5 and meta["fetched_pages"] == 1 and meta["complete"] is True
    rows, meta = run(engine, sql)
    assert meta["cache"] == "hit" and meta["fetched_pages"] == 0
    # an ORDER BY query needs everything, so it resumes the slice
    rows, meta = run(engine, sql.replace("LIMIT 5", "ORDER BY list_price LIMIT 5"))
    assert meta["fetched_pages"] == 2 and meta["complete"] is True


def test_failed_fetch_with_cached_rows_returns_data_and_error(tmp_path):
    src = FakeSource({("tempe, az", "for_sale"): [home(i) for i in range(300)]})
    e = Engine(store=Store(tmp_path / "c.duckdb"), source_factory=src)
    run(e, "SELECT 1 FROM properties WHERE location = 'Tempe, AZ' AND status = 'for_sale'", max_pages=1)
    src.fail = "Realtor.com blocked the request (403)."
    rows, meta = run(e, "SELECT count(*) n FROM properties WHERE location = 'Tempe, AZ' AND status = 'for_sale'")
    assert rows[0]["n"] == 200 and meta["complete"] is False and "403" in meta["errors"][0]


def test_foreclosure_not_served_from_unfiltered(engine, tempe):
    run(engine, "SELECT 1 FROM properties WHERE location = 'Mesa, AZ' AND status = 'for_sale'")
    _, meta = run(engine, "SELECT count(*) FROM properties WHERE location = 'Mesa, AZ' AND status = 'for_sale' AND is_foreclosure")
    assert meta["cache"] == "miss"


def test_cte_named_like_storage_table_rejected(engine):
    out = json.loads(engine.query("WITH t_slices AS (SELECT 1) SELECT * FROM t_slices", settings=Settings()))
    assert "t_" in out["error"]


def _documented_examples():
    import re
    from pathlib import Path

    from homeharvest_mcp.describe import EXAMPLES
    sqls = [line.strip()[2:] for line in EXAMPLES.splitlines() if line.strip().startswith("- SELECT")]
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
    block = readme.split("## Example queries", 1)[1].split("## Schema", 1)[0]
    for chunk in re.findall(r"```sql\n(.*?)```", block, re.S):
        stmt = []
        for line in chunk.splitlines():
            if line.startswith("--"):
                if stmt:
                    sqls.append(" ".join(stmt))
                stmt = []
            elif line.strip():
                stmt.append(line.strip())
        if stmt:
            sqls.append(" ".join(stmt))
    return sqls


@pytest.mark.parametrize("sql", _documented_examples())
def test_documented_examples_run(tmp_path, sql):
    class AnySource(FakeSource):
        def fetch_page(self, spec, loc, offset):
            st = spec.status
            rows = [home(i, status=st if st != "pending" else "for_sale", pending=st == "pending",
                         sold_date="2026-11-05" if st == "sold" else None, open_house=True) for i in range(5)]
            return Page(total=5, homes=rows if offset == 0 else [], raw_count=5 if offset == 0 else 0)

        def resolve(self, spec):
            return Location(text=spec.location, area_type="address" if spec.radius else "city",
                            centroid={"lat": 33.4, "lon": -111.9})

    e = Engine(store=Store(tmp_path / "c.duckdb"), source_factory=AnySource({}))
    out = e.query(sql, settings=Settings())
    meta = json.loads(out.splitlines()[-1])
    assert "error" not in meta, (sql, meta)
