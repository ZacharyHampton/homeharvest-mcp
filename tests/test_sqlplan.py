"""SQL parsing, safety, and pushdown extraction. No network."""

from datetime import date

import pytest

from homeharvest_mcp.sqlplan import QueryError, month_windows, plan_query

TODAY = date(2026, 11, 23)


def plan(sql):
    return plan_query(sql, today=TODAY)


def keys(p):
    return [s.key for s in p.specs]


# --- read-only + allowlist -------------------------------------------------

@pytest.mark.parametrize("sql", [
    "DELETE FROM properties",
    "DROP TABLE t_properties",
    "INSERT INTO properties VALUES (1)",
    "UPDATE properties SET beds = 1",
    "CREATE TABLE x AS SELECT 1",
    "COPY properties TO '/tmp/x.csv'",
    "ATTACH '/tmp/x.db' AS x",
    "PRAGMA database_list",
    "SET enable_external_access = true",
])
def test_rejects_non_select(sql):
    with pytest.raises(QueryError, match="Read-only|syntax|one SELECT"):
        plan(sql)


def test_rejects_multiple_statements():
    with pytest.raises(QueryError, match="one SELECT"):
        plan("SELECT 1 FROM properties WHERE location='x'; SELECT 2 FROM properties WHERE location='y'")


@pytest.mark.parametrize("sql,msg", [
    ("SELECT * FROM read_csv('/etc/hosts')", "Table functions"),
    ("SELECT * FROM read_parquet('s3://x/y.parquet')", "Table functions"),
    ("SELECT * FROM '/etc/passwd'", "Unknown table"),
    ("SELECT * FROM t_properties", "Unknown table"),
    ("SELECT * FROM main.properties WHERE location = 'Phoenix, AZ'", "bare table names"),
    ("SELECT * FROM duckdb_settings()", "Table functions"),
])
def test_rejects_files_and_internal_tables(sql, msg):
    with pytest.raises(QueryError, match=msg):
        plan(sql)


def test_cte_names_allowed():
    p = plan("WITH s AS (SELECT * FROM properties WHERE location = 'Phoenix, AZ' AND status = 'sold' "
             "AND sold_date >= '2026-11-01') SELECT count(*) FROM s")
    assert p.tables == {"properties"}
    assert keys(p) == ["phoenix, az|sold|2026-11-01..2026-11-30|"]


def test_syntax_error_is_reported():
    with pytest.raises(QueryError, match="syntax"):
        plan("SELEC * FROM properties")


# --- location ----------------------------------------------------------------

def test_location_required_with_teaching_message():
    with pytest.raises(QueryError, match=r"location = 'Phoenix, AZ'"):
        plan("SELECT * FROM properties WHERE status = 'sold'")


def test_location_is_stripped_from_sql():
    p = plan("SELECT count(*) FROM properties WHERE location = 'Phoenix, AZ' AND beds >= 3")
    assert "location" not in p.sql.lower().split("where")[1]
    assert "beds >= 3" in p.sql


def test_location_in_list_fetches_each():
    p = plan("SELECT location, count(*) FROM properties WHERE location IN ('Tempe, AZ', 'Mesa, AZ') "
             "AND status = 'for_sale' GROUP BY 1")
    assert keys(p) == ["tempe, az|for_sale|all|", "mesa, az|for_sale|all|"]


def test_location_under_or_is_rejected():
    with pytest.raises(QueryError, match="top-level AND"):
        plan("SELECT * FROM properties WHERE location = 'Tempe, AZ' OR beds > 3")


def test_location_like_is_rejected():
    with pytest.raises(QueryError, match="top-level AND"):
        plan("SELECT * FROM properties WHERE location LIKE 'Tempe%'")


def test_zip_and_city_state_become_locations():
    assert keys(plan("SELECT * FROM properties WHERE zip IN ('85004', '85008') AND status = 'sold'")) == [
        "85004|sold|all|", "85008|sold|all|"]
    assert keys(plan("SELECT * FROM properties WHERE city = 'Phoenix' AND state = 'AZ' AND status = 'pending'")) == [
        "phoenix, az|pending|all|"]


def test_city_and_zip_stay_as_local_filters():
    p = plan("SELECT * FROM properties WHERE zip = '85004' AND status = 'sold'")
    assert "zip = '85004'" in p.sql


def test_location_inside_cte_and_subquery():
    p = plan("SELECT * FROM agents WHERE property_id IN (SELECT property_id FROM properties "
             "WHERE location = 'Tempe, AZ' AND status = 'for_sale')")
    assert keys(p) == ["tempe, az|for_sale|all|"]


# --- status + dates ----------------------------------------------------------

def test_status_in_runs_one_search_per_status():
    p = plan("SELECT status, count(*) FROM properties WHERE location = 'Tempe, AZ' "
             "AND status IN ('for_sale', 'pending', 'sold') GROUP BY 1")
    assert [s.status for s in p.specs] == ["for_sale", "pending", "sold"]


def test_unknown_status():
    with pytest.raises(QueryError, match="Unknown status"):
        plan("SELECT * FROM properties WHERE location = 'Tempe, AZ' AND status = 'active'")


def test_sold_date_splits_into_months_newest_first():
    p = plan("SELECT * FROM properties WHERE location = 'Phoenix, AZ' AND status = 'sold' AND sold_date >= '2026-09-15'")
    assert [s.bucket for s in p.specs] == [
        "2026-11-01..2026-11-30", "2026-10-01..2026-10-31", "2026-09-01..2026-09-30"]


def test_between_and_upper_bound():
    p = plan("SELECT * FROM properties WHERE location = 'Phoenix, AZ' AND status = 'sold' "
             "AND sold_date BETWEEN '2026-01-10' AND '2026-02-05'")
    assert [s.bucket for s in p.specs] == ["2026-02-01..2026-02-28", "2026-01-01..2026-01-31"]


def test_relative_dates_are_folded():
    p = plan("SELECT * FROM properties WHERE location = 'Phoenix, AZ' AND status = 'sold' "
             "AND sold_date >= DATE '2026-11-23' - INTERVAL 30 DAY")
    assert [s.bucket for s in p.specs] == ["2026-11-01..2026-11-30", "2026-10-01..2026-10-31"]


def test_date_filter_applies_per_status():
    p = plan("SELECT * FROM properties WHERE location = 'Tempe, AZ' AND status IN ('sold', 'for_sale') "
             "AND sold_date >= '2026-11-01'")
    by = {s.status: s.bucket for s in p.specs}
    assert by == {"sold": "2026-11-01..2026-11-30", "for_sale": "all"}


def test_status_inferred_from_date_column():
    p = plan("SELECT * FROM properties WHERE location = 'Tempe, AZ' AND sold_date >= '2026-11-01'")
    assert [s.status for s in p.specs] == ["sold"]
    assert any("No status filter" in n for n in p.notes)


def test_default_status_is_for_sale():
    p = plan("SELECT * FROM properties WHERE location = 'Tempe, AZ'")
    assert [s.status for s in p.specs] == ["for_sale"]


def test_pending_date_is_local_only():
    p = plan("SELECT * FROM properties WHERE location = 'Tempe, AZ' AND status = 'pending' "
             "AND pending_date >= '2026-11-01'")
    assert keys(p) == ["tempe, az|pending|all|"]
    assert any("pending_date" in n for n in p.notes)


def test_upper_bound_only_is_local():
    p = plan("SELECT * FROM properties WHERE location = 'Tempe, AZ' AND status = 'sold' AND sold_date < '2026-01-01'")
    assert keys(p) == ["tempe, az|sold|all|"]


def test_or_dates_not_pushed():
    p = plan("SELECT * FROM properties WHERE location = 'Tempe, AZ' AND status = 'sold' "
             "AND (sold_date >= '2026-11-01' OR beds > 4)")
    assert keys(p) == ["tempe, az|sold|all|"]


def test_future_window_dropped():
    p = plan("SELECT * FROM properties WHERE location = 'Tempe, AZ' AND status = 'sold' AND sold_date >= '2027-01-01'")
    assert p.specs == []


def test_month_windows():
    assert month_windows(date(2025, 12, 5), date(2026, 1, 3)) == [
        (date(2026, 1, 1), date(2026, 1, 31)), (date(2025, 12, 1), date(2025, 12, 31))]


# --- numeric + type pushdown --------------------------------------------------

def test_numeric_bounds_are_supersets():
    p = plan("SELECT * FROM properties WHERE location = 'Tempe, AZ' AND status = 'for_sale' AND beds > 2 "
             "AND sqft BETWEEN 1500 AND 2500 AND list_price <= 600000.5 AND year_built >= 1999.5 AND lot_sqft < 8000")
    f = p.specs[0].filter_dict
    assert f == {"beds_min": 3, "sqft_min": 1500, "sqft_max": 2500, "price_max": 600000,
                 "year_built_min": 2000, "lot_sqft_max": 7999}


def test_reversed_comparison():
    p = plan("SELECT * FROM properties WHERE location = 'Tempe, AZ' AND 3 <= beds")
    assert p.specs[0].filter_dict == {"beds_min": 3}


def test_list_price_not_pushed_for_sold():
    p = plan("SELECT * FROM properties WHERE location = 'Tempe, AZ' AND status = 'sold' AND list_price > 500000")
    assert p.specs[0].filter_dict == {}


def test_baths_and_sold_price_stay_local():
    p = plan("SELECT * FROM properties WHERE location = 'Tempe, AZ' AND status = 'sold' AND baths >= 2 AND sold_price > 1")
    assert p.specs[0].filters == ()
    assert "baths >= 2" in p.sql


def test_property_type_pushdown():
    p = plan("SELECT * FROM properties WHERE location = 'Tempe, AZ' AND property_type IN ('condos', 'townhomes')")
    assert p.specs[0].filter_dict == {"property_type": "condos,townhomes"}
    p = plan("SELECT * FROM properties WHERE location = 'Tempe, AZ' AND property_type = 'coop'")
    assert p.specs[0].filter_dict == {}


def test_foreclosure_pushdown():
    for cond in ("is_foreclosure = true", "is_foreclosure", "is_foreclosure IS TRUE"):
        p = plan(f"SELECT * FROM properties WHERE location = 'Tempe, AZ' AND {cond}")
        assert p.specs[0].filter_dict == {"foreclosure": True}, cond


def test_filtered_slices_have_distinct_keys():
    a = plan("SELECT * FROM properties WHERE location = 'Tempe, AZ' AND beds >= 3").specs[0]
    b = plan("SELECT * FROM properties WHERE location = 'Tempe, AZ'").specs[0]
    assert a.key != b.key and a.bucket == b.bucket


# --- radius + addresses ---------------------------------------------------------

def test_radius_needs_address():
    with pytest.raises(QueryError, match="street address"):
        plan("SELECT * FROM properties WHERE location = 'Phoenix, AZ' AND radius = 1")


def test_radius_with_address_single_window():
    p = plan("SELECT * FROM properties WHERE location = '407 E Honda Bow Rd, Phoenix, AZ 85086' AND radius = 0.5 "
             "AND status = 'sold' AND sold_date >= '2026-06-01'")
    assert len(p.specs) == 1
    s = p.specs[0]
    assert s.radius == 0.5 and s.date_from == date(2026, 6, 1) and s.date_to == TODAY
    assert "radius" not in p.sql.lower()


def test_radius_outside_where_rejected():
    with pytest.raises(QueryError, match="radius"):
        plan("SELECT radius FROM properties WHERE location = 'Tempe, AZ'")


def test_address_without_radius_is_single_home():
    p = plan("SELECT * FROM properties WHERE location = '407 E Honda Bow Rd, Phoenix, AZ 85086'")
    assert [s.status for s in p.specs] == ["any"]
    assert not any("No status filter" in n for n in p.notes)


# --- history tables -----------------------------------------------------------

def test_history_by_property_id_needs_no_location():
    p = plan("SELECT * FROM price_history WHERE property_id IN ('1', '2')")
    assert p.specs == [] and p.history_ids == ["1", "2"]


def test_history_without_ids_or_location():
    with pytest.raises(QueryError, match="property_id"):
        plan("SELECT * FROM tax_history")


def test_history_join_uses_listing_scope():
    p = plan("SELECT h.* FROM price_history h JOIN properties p USING (property_id) "
             "WHERE location = 'Tempe, AZ' AND p.status = 'sold' AND p.sold_date >= '2026-11-01'")
    assert p.needs_history and keys(p) == ["tempe, az|sold|2026-11-01..2026-11-30|"]


# --- SQL is preserved ------------------------------------------------------------

def test_duckdb_functions_survive_roundtrip():
    p = plan("SELECT zip, median(sold_price) AS med, quantile_cont(sqft, 0.9) q, count(*) n FROM properties "
             "WHERE location = 'Phoenix, AZ' AND status = 'sold' AND sold_date >= '2026-10-01' "
             "AND list_contains(tags, 'pool') GROUP BY ALL ORDER BY med DESC LIMIT 10")
    sql = p.sql.upper()
    for token in ("MEDIAN(SOLD_PRICE)", "QUANTILE_CONT", "GROUP BY ALL", "LIMIT 10", "'POOL'"):
        assert token in sql


def test_limit_hint_only_for_plain_selects():
    base = "FROM properties WHERE location = 'Tempe, AZ' AND status = 'for_sale'"
    assert plan(f"SELECT address {base} LIMIT 5").limit_hint == 5
    assert plan(f"SELECT address {base} AND beds > 3 LIMIT 5").limit_hint == 5
    for q in (f"SELECT address {base} ORDER BY list_price LIMIT 5",
              f"SELECT count(*) {base} LIMIT 5",
              f"SELECT zip, count(*) {base} GROUP BY zip LIMIT 5",
              f"SELECT DISTINCT zip {base} LIMIT 5",
              f"SELECT address {base}",
              f"SELECT p.address FROM properties p JOIN agents a USING (property_id) WHERE location = 'Tempe, AZ' LIMIT 5"):
        assert plan(q).limit_hint is None, q
