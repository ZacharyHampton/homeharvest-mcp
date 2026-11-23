"""Table definitions: our own column names, types, and descriptions.

Storage tables are prefixed with ``t_`` and carry a ``slice_id``; the names an
agent queries (``properties``, ``agents`` ...) are per-query views scoped to the
slices that match the query's location/status/date filters.
"""

from __future__ import annotations

import json
from importlib import resources

# (name, duckdb type, description)
PROPERTIES = [
    ("property_id", "VARCHAR", "Key. Stable id for the home; join key for every other table."),
    ("status", "VARCHAR", "for_sale | pending | sold | for_rent (pending/contingent homes found in a for_sale search are labeled pending)."),
    ("location", "VARCHAR", "The search location this row came from (lowercased). Filter on it to choose what to fetch."),
    ("mls_status", "VARCHAR", "Raw MLS status, e.g. Active, Pending, Sold, Coming Soon."),
    ("list_price", "BIGINT", "Asking price (for_sale/pending) or last list price (sold)."),
    ("list_price_min", "BIGINT", "Low end of a price range (rentals, new builds)."),
    ("list_price_max", "BIGINT", "High end of a price range (rentals, new builds)."),
    ("list_date", "TIMESTAMP", "When the listing went live."),
    ("pending_date", "TIMESTAMP", "When the listing went pending."),
    ("sold_price", "BIGINT", "Closing price. Only set when status = 'sold'."),
    ("sold_date", "DATE", "Closing date. Only set when status = 'sold'."),
    ("last_sold_price", "BIGINT", "Most recent recorded sale price (any status; for active listings this is the prior sale)."),
    ("last_sold_date", "DATE", "Most recent recorded sale date (any status)."),
    ("last_status_change_date", "TIMESTAMP", "Last status change."),
    ("last_update_date", "TIMESTAMP", "Last time the listing was updated."),
    ("days_on_mls", "INTEGER", "Days from list_date to sold_date (sold) or to today (active). Computed."),
    ("price_per_sqft", "INTEGER", "Price per square foot as reported by the source."),
    ("estimated_value", "BIGINT", "Best automated valuation (AVM) for the home."),
    ("estimated_value_low", "BIGINT", "Low end of the best AVM range."),
    ("estimated_value_high", "BIGINT", "High end of the best AVM range."),
    ("hoa_fee", "INTEGER", "Monthly HOA fee (0 = none reported)."),
    ("beds", "INTEGER", "Bedrooms."),
    ("baths", "DOUBLE", "Bathrooms: full + 0.5 x half."),
    ("baths_full", "INTEGER", "Full bathrooms."),
    ("baths_half", "INTEGER", "Half bathrooms."),
    ("sqft", "INTEGER", "Interior living area, square feet."),
    ("lot_sqft", "INTEGER", "Lot size, square feet."),
    ("year_built", "INTEGER", "Year built."),
    ("stories", "INTEGER", "Stories."),
    ("garage", "DOUBLE", "Garage spaces."),
    ("property_type", "VARCHAR", "single_family | condos | townhomes | multi_family | land | mobile | farm | apartment | duplex_triplex | ..."),
    ("is_new_construction", "BOOLEAN", "New construction flag."),
    ("is_foreclosure", "BOOLEAN", "TRUE when fetched with WHERE is_foreclosure = true; otherwise NULL (unknown)."),
    ("is_pending", "BOOLEAN", "Pending flag from the source."),
    ("is_contingent", "BOOLEAN", "Contingent flag from the source."),
    ("has_open_house", "BOOLEAN", "TRUE when the open_houses table has at least one row for this home."),
    ("address", "VARCHAR", "Full one-line address."),
    ("street", "VARCHAR", "Street line, e.g. '407 E Honda Bow Rd'."),
    ("unit", "VARCHAR", "Unit number."),
    ("city", "VARCHAR", "City."),
    ("state", "VARCHAR", "Two-letter state code."),
    ("zip", "VARCHAR", "5-digit ZIP code."),
    ("county", "VARCHAR", "County name."),
    ("fips_code", "VARCHAR", "County FIPS code."),
    ("neighborhoods", "VARCHAR", "Neighborhood names, comma-separated."),
    ("lat", "DOUBLE", "Latitude."),
    ("lon", "DOUBLE", "Longitude."),
    ("mls", "VARCHAR", "MLS source code, e.g. PHAZ."),
    ("mls_id", "VARCHAR", "Listing number inside that MLS."),
    ("listing_id", "VARCHAR", "Listing id (changes each time the home is re-listed)."),
    ("agent_id", "VARCHAR", "Listing agent id; hop to agents."),
    ("agent_name", "VARCHAR", "Listing agent name."),
    ("office_id", "VARCHAR", "Listing office id; hop to offices."),
    ("office_name", "VARCHAR", "Listing office name."),
    ("tags", "VARCHAR[]", "Feature tags, e.g. ['pool','single_story']. Use list_contains(tags, 'pool')."),
    ("url", "VARCHAR", "Listing page URL."),
    ("primary_photo", "VARCHAR", "Primary photo URL."),
    ("description", "VARCHAR", "Listing remarks (long text; cut to 1,000 chars in results)."),
    ("fetched_at", "TIMESTAMP", "When this row was fetched (cache age)."),
]

AGENTS = [
    ("agent_id", "VARCHAR", "Agent id (stable across listings)."),
    ("property_id", "VARCHAR", "The listing this agent is on; hop to properties."),
    ("status", "VARCHAR", "That listing's status (for_sale | pending | sold | for_rent)."),
    ("role", "VARCHAR", "seller (listing side) or buyer, as reported."),
    ("name", "VARCHAR", "Agent name."),
    ("email", "VARCHAR", "Agent email."),
    ("phone", "VARCHAR", "Primary phone, digits only."),
    ("phones", "VARCHAR[]", "All phones as 'digits (type)'."),
    ("office_id", "VARCHAR", "Office id; hop to offices."),
    ("office_name", "VARCHAR", "Office name."),
    ("broker_name", "VARCHAR", "Brokerage name."),
    ("nrds_id", "VARCHAR", "NAR NRDS id."),
    ("mls_set", "VARCHAR", "Agent's MLS membership code."),
    ("state_license", "VARCHAR", "State license number."),
]

OFFICES = [
    ("office_id", "VARCHAR", "Office id."),
    ("status", "VARCHAR", "Status of the listings counted in this row."),
    ("name", "VARCHAR", "Office name."),
    ("email", "VARCHAR", "Office email."),
    ("phone", "VARCHAR", "Primary office phone, digits only."),
    ("phones", "VARCHAR[]", "All phones as 'digits (type)'."),
    ("mls_set", "VARCHAR", "Office MLS membership code."),
    ("listings", "BIGINT", "Listings in this query's scope carried by this office."),
    ("property_ids", "VARCHAR[]", "Those listings' property_ids."),
]

OPEN_HOUSES = [
    ("property_id", "VARCHAR", "Hop to properties."),
    ("status", "VARCHAR", "That listing's status."),
    ("start_time", "TIMESTAMP", "Open house start, local time at the home (see time_zone)."),
    ("end_time", "TIMESTAMP", "Open house end, local time."),
    ("time_zone", "VARCHAR", "Time zone of start/end, e.g. MST, EST."),
    ("description", "VARCHAR", "Notes from the listing agent."),
    ("methods", "VARCHAR[]", "e.g. ['in_person'] or ['virtual']."),
]

PRICE_HISTORY = [
    ("property_id", "VARCHAR", "Hop to properties."),
    ("date", "DATE", "Event date."),
    ("event", "VARCHAR", "Listed | Price Changed | Pending | Sold | Listing removed | ..."),
    ("price", "BIGINT", "Price at the event."),
    ("price_per_sqft", "DOUBLE", "Price per square foot at the event."),
    ("source", "VARCHAR", "Data source (MLS or public record)."),
]

TAX_HISTORY = [
    ("property_id", "VARCHAR", "Hop to properties."),
    ("year", "INTEGER", "Tax year."),
    ("tax", "BIGINT", "Tax billed."),
    ("assessed_total", "BIGINT", "Total assessed value."),
    ("assessed_building", "BIGINT", "Assessed building value."),
    ("assessed_land", "BIGINT", "Assessed land value."),
]

TABLES: dict[str, dict] = {
    "properties": {
        "grain": "1 row = 1 home in 1 status",
        "key": "property_id",
        "columns": PROPERTIES,
        "purpose": "Listings: for sale, pending, sold, and rentals.",
    },
    "agents": {
        "grain": "1 row = 1 agent on 1 listing",
        "key": "(agent_id, property_id)",
        "columns": AGENTS,
        "purpose": "Listing agents with email and phone. Comes with properties at no extra cost.",
    },
    "offices": {
        "grain": "1 row = 1 office x status (rolled up over the listings in scope)",
        "key": "(office_id, status)",
        "columns": OFFICES,
        "purpose": "Brokerage offices with listing counts.",
    },
    "open_houses": {
        "grain": "1 row = 1 scheduled open house",
        "key": "(property_id, start_time)",
        "columns": OPEN_HOUSES,
        "purpose": "Upcoming open houses. Comes with properties at no extra cost.",
    },
    "price_history": {
        "grain": "1 row = 1 listing/sale event",
        "key": "(property_id, date, event)",
        "columns": PRICE_HISTORY,
        "purpose": "Listing and sale events over time. Costs 1 request per 50 homes (fetched on first use, then cached).",
    },
    "tax_history": {
        "grain": "1 row = 1 tax year",
        "key": "(property_id, year)",
        "columns": TAX_HISTORY,
        "purpose": "Property tax and assessed value by year. Costs 1 request per 50 homes (fetched on first use, then cached).",
    },
}

HISTORY_TABLES = {"price_history", "tax_history"}
LISTING_TABLES = {"properties", "agents", "offices", "open_houses"}
STATUSES = ("for_sale", "pending", "sold", "for_rent")
DATE_COLUMN_FOR_STATUS = {"sold": "sold_date", "for_sale": "list_date", "for_rent": "list_date", "pending": "pending_date"}

# Columns stored in t_* tables (views add/remove a few).
STORED_COLUMNS = {
    "properties": [c for c in PROPERTIES if c[0] not in ("has_open_house", "location")],
    "agents": AGENTS,
    "offices": [c for c in OFFICES if c[0] not in ("listings", "property_ids")] + [("property_id", "VARCHAR", "")],
    "open_houses": OPEN_HOUSES,
    "price_history": PRICE_HISTORY,
    "tax_history": TAX_HISTORY,
}


def column_names(table: str) -> list[str]:
    return [c[0] for c in TABLES[table]["columns"]]


def load_fill_rates() -> dict:
    try:
        text = resources.files("homeharvest_mcp").joinpath("fill_rates.json").read_text()
        return json.loads(text)
    except (FileNotFoundError, ModuleNotFoundError, json.JSONDecodeError):
        return {}
