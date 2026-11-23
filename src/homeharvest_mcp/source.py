"""The one module that talks to HomeHarvest.

Everything that depends on HomeHarvest internals lives here, so a HomeHarvest
upgrade can only break this file. We use the scraper object directly (instead
of ``scrape_property``) for three reasons found while probing 0.8.18:

* ``general_search`` returns the source's ``total`` count, which the public API
  hides. We need it to know when a slice is complete and when to split a date
  window under the 10,000-row cap.
* ``extra_property_data=True`` asks for 200 homes in one GraphQL request, which
  the API rejects ("token limit reached"), so the extra details silently come
  back empty. We pass ``False`` and fetch price/tax history ourselves in
  batches of 50, and only when a query needs it.
* One location lookup per slice instead of one per page.

If the private surface changes, we fall back to the public ``scrape_property``
(per-page, no totals).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

PAGE_SIZE = 200
MAX_OFFSET = 10_000  # source rule: offset + limit <= 10,000
HISTORY_BATCH = 50  # the API caps root fields per request; 50 works, 100 does not

HISTORY_QUERY_FIELDS = (
    "{ property_id property_history { date event_name price price_sqft source_name } "
    "tax_history { tax year assessment { building land total } } }"
)

ADDRESS_RE = re.compile(r"^\s*\d+[A-Za-z]?\s+\S+.*[A-Za-z]")


class SourceError(Exception):
    """An error the agent can act on (message is shown verbatim)."""


def looks_like_address(location: str) -> bool:
    return bool(ADDRESS_RE.match(location)) and not location.strip().isdigit()


def normalize_location(location: str) -> str:
    return re.sub(r"\s+", " ", location.strip().strip(",;")).lower()


@dataclass(frozen=True)
class SliceSpec:
    """One cacheable unit of fetching: a location, one status, one date window, one filter set."""

    location: str
    status: str  # for_sale | pending | sold | for_rent | any (single address)
    date_from: date | None = None
    date_to: date | None = None
    filters: tuple = ()  # sorted (name, value) pairs pushed to the source
    radius: float | None = None

    @property
    def location_key(self) -> str:
        return normalize_location(self.location)

    @property
    def bucket(self) -> str:
        if self.date_from or self.date_to:
            return f"{self.date_from or ''}..{self.date_to or ''}"
        return "all"

    @property
    def filter_sig(self) -> str:
        parts = [f"{k}={v}" for k, v in self.filters]
        if self.radius:
            parts.append(f"radius={self.radius}")
        return "&".join(parts)

    @property
    def key(self) -> str:
        return f"{self.location_key}|{self.status}|{self.bucket}|{self.filter_sig}"

    @property
    def filter_dict(self) -> dict:
        return dict(self.filters)


@dataclass
class Page:
    total: int | None
    homes: list[dict]
    raw_count: int  # rows the source returned before our own filtering


@dataclass
class Location:
    text: str
    area_type: str | None
    centroid: dict | None = None
    mpr_id: str | None = None
    extra: dict = field(default_factory=dict)


def _hh():
    """Import HomeHarvest lazily so unit tests never need the network stack."""
    from homeharvest.core.scrapers import ScraperInput
    from homeharvest.core.scrapers.models import ListingType, ReturnType, SearchPropertyType
    from homeharvest.core.scrapers.realtor import RealtorScraper

    return ScraperInput, RealtorScraper, ListingType, ReturnType, SearchPropertyType


def _capture_errors(scraper) -> list[str]:
    """general_search swallows GraphQL errors (returns 0 rows); record them so an
    error never looks like an empty, complete result."""
    errors: list[str] = []
    original = scraper._graphql_post

    def wrapped(query, variables, operation_name):
        data = original(query, variables, operation_name)
        if isinstance(data, dict) and data.get("errors"):
            errors.extend(str((e or {}).get("message", e)) for e in data["errors"])
        return data

    scraper._graphql_post = wrapped
    return errors


def _id(v) -> str | None:
    """Source ids; '0' is a placeholder shared by unrelated offices/agents."""
    if v is None:
        return None
    s = str(v).strip()
    return None if s in ("", "0", "None") else s


def _friendly(exc: Exception) -> SourceError:
    name = type(exc).__name__
    msg = str(exc)
    if name == "AuthenticationError" or "403" in msg:
        return SourceError(
            "Realtor.com blocked the request (403). Wait a minute and retry, or set a proxy "
            "(env HOMEHARVEST_PROXY, or proxy_url in the server config)."
        )
    if "ProxyError" in msg or "ProxyError" in name:
        return SourceError("Could not connect through the proxy. Check proxy_url (or HOMEHARVEST_PROXY), e.g. http://user:pass@host:port.")
    if name == "RateLimitError" or "429" in msg:
        return SourceError("Realtor.com is rate limiting (429). Wait a minute, or set a proxy.")
    return SourceError(f"Fetch failed: {name}: {msg[:300]}")


class Source:
    def __init__(self, proxy: str | None = None):
        self.proxy = proxy or None

    # -- scraper construction -------------------------------------------------
    def _scraper(self, spec: SliceSpec, sort_by: str | None = None):
        ScraperInput, RealtorScraper, ListingType, ReturnType, SearchPropertyType = _hh()
        f = spec.filter_dict
        lt = None if spec.status == "any" else ListingType[spec.status.upper()]
        kwargs: dict[str, Any] = dict(
            location=spec.location,
            listing_type=lt,
            return_type=ReturnType.raw,
            proxy=self.proxy,
            radius=spec.radius,
            foreclosure=bool(f.get("foreclosure")) or None,
            extra_property_data=False,
            limit=MAX_OFFSET,
            offset=0,
            parallel=False,
            sort_by=sort_by,
            sort_direction="desc",
        )
        if spec.date_from or spec.date_to:
            # The source only builds a date filter when BOTH ends are set.
            kwargs["date_from"] = (spec.date_from or date(1990, 1, 1)).isoformat()
            kwargs["date_to"] = (spec.date_to or date.today()).isoformat()
            kwargs["date_from_precision"] = "day"
            kwargs["date_to_precision"] = "day"
        for name in (
            "beds_min", "beds_max", "sqft_min", "sqft_max", "price_min", "price_max",
            "lot_sqft_min", "lot_sqft_max", "year_built_min", "year_built_max",
        ):
            if name in f:
                kwargs[name] = f[name]
        if "property_type" in f:
            kwargs["property_type"] = [SearchPropertyType(v) for v in f["property_type"].split(",")]
        return RealtorScraper(ScraperInput(**kwargs))

    # -- public API -----------------------------------------------------------
    def resolve(self, spec: SliceSpec) -> Location:
        try:
            s = self._scraper(spec)
            info = s.handle_location()
        except Exception as e:  # noqa: BLE001
            raise _friendly(e) from e
        if not info:
            raise SourceError(
                f"Location not found: '{spec.location}'. Use 'City, ST', a 5-digit ZIP, a county "
                "('Maricopa County, AZ'), a neighborhood, or a full street address."
            )
        return Location(
            text=info.get("text") or spec.location,
            area_type=info.get("area_type"),
            centroid=info.get("centroid"),
            mpr_id=info.get("mpr_id"),
        )

    def fetch_page(self, spec: SliceSpec, loc: Location, offset: int) -> Page:
        # Stable sort so resumed paging lines up. (pending_date is not a valid sort field.)
        sort_by = {"for_sale": "list_date", "for_rent": "list_date", "pending": "list_date"}.get(spec.status)
        try:
            s = self._scraper(spec, sort_by=sort_by)
            api_errors = _capture_errors(s)
            variables: dict[str, Any] = {"offset": offset}
            if spec.radius:
                if not loc.centroid:
                    raise SourceError(f"'{spec.location}' has no map point, so radius search can't run.")
                variables["coordinates"] = [loc.centroid["lon"], loc.centroid["lat"]]
                variables["radius"] = f"{spec.radius}mi"
                search_type = "comps"
            else:
                variables["search_location"] = {"location": loc.text}
                search_type = "area"
            if spec.filter_dict.get("foreclosure"):
                variables["foreclosure"] = True
            result = s.general_search(variables, search_type=search_type)
        except SourceError:
            raise
        except (AttributeError, TypeError):
            return self._fetch_page_public(spec, offset)
        except Exception as e:  # noqa: BLE001
            raise _friendly(e) from e
        homes = list(result.get("properties") or [])
        if not homes and api_errors:
            raise SourceError(f"Source rejected the search: {api_errors[0][:300]}")
        return Page(total=result.get("total"), homes=homes, raw_count=len(homes))

    def _fetch_page_public(self, spec: SliceSpec, offset: int) -> Page:
        """Fallback through the public API if HomeHarvest internals moved."""
        from homeharvest import scrape_property

        f = spec.filter_dict
        kwargs: dict[str, Any] = dict(
            location=spec.location, listing_type=None if spec.status == "any" else spec.status,
            return_type="raw", radius=spec.radius, proxy=self.proxy, extra_property_data=False,
            limit=PAGE_SIZE, offset=offset, parallel=False, foreclosure=f.get("foreclosure"),
        )
        if spec.date_from or spec.date_to:
            kwargs["date_from"] = (spec.date_from or date(1990, 1, 1)).isoformat()
            kwargs["date_to"] = (spec.date_to or date.today()).isoformat()
        for k, v in f.items():
            if k == "property_type":
                kwargs[k] = v.split(",")
            elif k != "foreclosure":
                kwargs[k] = v
        try:
            homes = scrape_property(**kwargs)
        except Exception as e:  # noqa: BLE001
            raise _friendly(e) from e
        homes = list(homes or [])
        return Page(total=None, homes=homes, raw_count=len(homes))

    def fetch_home(self, spec: SliceSpec, loc: Location) -> list[dict]:
        """Single street address, no radius: one detailed record."""
        try:
            s = self._scraper(spec)
            return [h for h in (s.handle_home(loc.mpr_id) or []) if isinstance(h, dict)]
        except Exception as e:  # noqa: BLE001
            raise _friendly(e) from e

    def fetch_histories(self, property_ids: list[str]) -> dict[str, dict]:
        """price_history + tax_history for many homes, 50 per request."""
        ids = [p for p in dict.fromkeys(property_ids) if p and str(p).isdigit()]
        out: dict[str, dict] = {}
        if not ids:
            return out
        try:
            s = self._scraper(SliceSpec(location="x", status="for_sale"))
            for i in range(0, len(ids), HISTORY_BATCH):
                batch = ids[i : i + HISTORY_BATCH]
                q = "query GetHomeHistory { %s }" % " ".join(
                    f"h{p}: home(property_id: {p}) {HISTORY_QUERY_FIELDS}" for p in batch
                )
                data = s._graphql_post(q, {}, "GetHomeHistory") or {}
                if not data.get("data") and data.get("errors"):
                    raise SourceError(f"Source rejected the history lookup: {str(data['errors'][0])[:300]}")
                for p in batch:
                    out[p] = (data.get("data") or {}).get(f"h{p}") or {}
        except Exception as e:  # noqa: BLE001
            if out:
                return out
            raise e if isinstance(e, SourceError) else _friendly(e) from e
        return out


# ---------------------------------------------------------------------------
# Flattening: source JSON -> our tables
# ---------------------------------------------------------------------------

def _get(d: Any, *path, default=None):
    for p in path:
        if not isinstance(d, dict):
            return default
        d = d.get(p)
        if d is None:
            return default
    return d


def _ts(v) -> datetime | None:
    if not v:
        return None
    if isinstance(v, datetime):
        return v.astimezone(timezone.utc).replace(tzinfo=None) if v.tzinfo else v
    s = str(v).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.strptime(s[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if dt.tzinfo:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _d(v) -> date | None:
    t = _ts(v)
    return t.date() if t else None


def _int(v) -> int | None:
    try:
        return None if v is None else int(round(float(v)))
    except (TypeError, ValueError):
        return None


def _phones(phones) -> tuple[str | None, list[str]]:
    if isinstance(phones, dict):
        phones = [phones]
    items = []
    primary = None
    for p in phones or []:
        num = re.sub(r"\D", "", str(p.get("number") or ""))
        if not num:
            continue
        label = f"{num} ({p.get('type') or 'unknown'})"
        if label not in items:
            items.append(label)
        if primary is None and p.get("primary"):
            primary = num
    if primary is None and items:
        primary = items[0].split(" ")[0]
    return primary, items


def effective_status(raw: dict, requested: str) -> str:
    flags = raw.get("flags") or {}
    if requested == "for_sale" and (flags.get("is_pending") or flags.get("is_contingent")):
        return "pending"
    if requested == "any":
        s = (raw.get("status") or "").lower()
        if s == "for_sale" and (flags.get("is_pending") or flags.get("is_contingent")):
            return "pending"
        return s if s in ("for_sale", "sold", "for_rent", "pending") else (s or "off_market")
    return requested


def flatten(raw: dict, requested_status: str, fetched_at: datetime, foreclosure: bool = False) -> dict[str, list[dict]]:
    """One source home -> rows for properties / agents / offices / open_houses."""
    pid = str(raw.get("property_id") or "")
    status = effective_status(raw, requested_status)
    desc = raw.get("description") or {}
    addr = _get(raw, "location", "address") or {}
    coord = addr.get("coordinate") or {}
    flags = raw.get("flags") or {}

    est = None
    for e in raw.get("current_estimates") or _get(raw, "estimates", "currentValues") or []:
        if e and (est is None or e.get("isBestHomeValue")):
            est = e
            if e.get("isBestHomeValue"):
                break

    list_date = _ts(raw.get("list_date"))
    last_sold_date = _d(raw.get("last_sold_date"))
    sold_date = last_sold_date if status == "sold" else None
    sold_price = _int(raw.get("last_sold_price") or desc.get("sold_price")) if status == "sold" else None
    if status == "sold" and list_date and sold_date:
        dom = (sold_date - list_date.date()).days
    elif status in ("for_sale", "pending", "for_rent") and list_date:
        dom = (fetched_at.date() - list_date.date()).days
    else:
        dom = None
    if dom is not None and dom < 0:
        dom = None

    bf, bh = _int(desc.get("baths_full")), _int(desc.get("baths_half"))
    baths = None if bf is None and bh is None else (bf or 0) + 0.5 * (bh or 0)

    advertisers = raw.get("advertisers") or []
    seller = next((a for a in advertisers if (a or {}).get("type") == "seller"), advertisers[0] if advertisers else None) or {}
    seller_office = seller.get("office") or {}

    line = addr.get("line")
    city, state, zip_ = addr.get("city"), addr.get("state_code"), addr.get("postal_code")
    full = ", ".join(x for x in [line, city, " ".join(x for x in [state, zip_] if x)] if x) or None

    neighborhoods = _get(raw, "location", "neighborhoods")
    if isinstance(neighborhoods, list):
        neighborhoods = ", ".join(n.get("name", "") for n in neighborhoods if isinstance(n, dict) and n.get("name")) or None

    text = desc.get("text")
    open_houses = raw.get("open_houses") or []

    prop = {
        "property_id": pid,
        "status": status,
        "mls_status": raw.get("mls_status"),
        "list_price": _int(raw.get("list_price")),
        "list_price_min": _int(raw.get("list_price_min")),
        "list_price_max": _int(raw.get("list_price_max")),
        "list_date": list_date,
        "pending_date": _ts(raw.get("pending_date")),
        "sold_price": sold_price,
        "sold_date": sold_date,
        "last_sold_price": _int(raw.get("last_sold_price")),
        "last_sold_date": last_sold_date,
        "last_status_change_date": _ts(raw.get("last_status_change_date")),
        "last_update_date": _ts(raw.get("last_update_date")),
        "days_on_mls": dom,
        "price_per_sqft": _int(raw.get("price_per_sqft")),
        "estimated_value": _int((est or {}).get("estimate")),
        "estimated_value_low": _int((est or {}).get("estimateLow")),
        "estimated_value_high": _int((est or {}).get("estimateHigh")),
        "hoa_fee": _int(_get(raw, "hoa", "fee")),
        "beds": _int(desc.get("beds")),
        "baths": baths,
        "baths_full": bf,
        "baths_half": bh,
        "sqft": _int(desc.get("sqft")),
        "lot_sqft": _int(desc.get("lot_sqft")),
        "year_built": _int(desc.get("year_built")),
        "stories": _int(desc.get("stories")),
        "garage": float(desc["garage"]) if isinstance(desc.get("garage"), (int, float)) else None,
        "property_type": desc.get("type"),
        "is_new_construction": flags.get("is_new_construction"),
        "is_foreclosure": True if foreclosure else flags.get("is_foreclosure"),
        "is_pending": flags.get("is_pending"),
        "is_contingent": flags.get("is_contingent"),
        "address": full,
        "street": line,
        "unit": addr.get("unit"),
        "city": city,
        "state": state,
        "zip": zip_,
        "county": _get(raw, "location", "county", "name"),
        "fips_code": _get(raw, "location", "county", "fips_code"),
        "neighborhoods": neighborhoods if isinstance(neighborhoods, str) else None,
        "lat": coord.get("lat"),
        "lon": coord.get("lon"),
        "mls": _get(raw, "source", "id"),
        "mls_id": _get(raw, "source", "listing_id"),
        "listing_id": raw.get("listing_id"),
        "agent_id": _id(seller.get("fulfillment_id")) or _id(seller.get("nrds_id")),
        "agent_name": seller.get("name"),
        "office_id": _id(seller_office.get("fulfillment_id")),
        "office_name": seller_office.get("name"),
        "tags": list(raw.get("tags") or []) or None,
        "url": raw.get("href") or (f"https://www.realtor.com/realestateandhomes-detail/{raw['permalink']}" if raw.get("permalink") else None),
        "primary_photo": _get(raw, "primary_photo", "href"),
        "description": text,
        "fetched_at": fetched_at,
    }

    agents, offices = [], []
    seen_offices = set()
    for a in advertisers:
        if not a:
            continue
        office = a.get("office") or {}
        aid = _id(a.get("fulfillment_id")) or _id(a.get("nrds_id")) or (a.get("name") and f"name:{a['name']}")
        if aid:
            phone, phones = _phones(a.get("phones"))
            agents.append({
                "agent_id": str(aid),
                "property_id": pid,
                "role": a.get("type"),
                "name": a.get("name"),
                "email": a.get("email"),
                "phone": phone,
                "phones": phones or None,
                "office_id": _id(office.get("fulfillment_id")),
                "office_name": office.get("name"),
                "broker_name": _get(a, "broker", "name"),
                "nrds_id": a.get("nrds_id"),
                "mls_set": a.get("mls_set"),
                "state_license": a.get("state_license"),
            })
        oid = _id(office.get("fulfillment_id"))
        if oid and oid not in seen_offices:
            seen_offices.add(oid)
            ophone, ophones = _phones(office.get("phones"))
            offices.append({
                "office_id": str(oid),
                "name": office.get("name"),
                "email": office.get("email"),
                "phone": ophone,
                "phones": ophones or None,
                "mls_set": office.get("mls_set"),
                "property_id": pid,
            })

    ohs = []
    for oh in open_houses:
        if not oh:
            continue
        ohs.append({
            "property_id": pid,
            "start_time": _ts(oh.get("start_date")),
            "end_time": _ts(oh.get("end_date")),
            "time_zone": oh.get("time_zone"),
            "description": oh.get("description"),
            "methods": list(oh.get("methods") or []) or None,
        })

    return {"properties": [prop], "agents": agents, "offices": offices, "open_houses": ohs}


def flatten_history(pid: str, data: dict) -> dict[str, list[dict]]:
    ph = []
    for e in data.get("property_history") or []:
        if not e:
            continue
        ph.append({
            "property_id": pid,
            "date": _d(e.get("date")),
            "event": e.get("event_name"),
            "price": _int(e.get("price")),
            "price_per_sqft": round(float(e["price_sqft"]), 2) if isinstance(e.get("price_sqft"), (int, float)) else None,
            "source": e.get("source_name"),
        })
    th = []
    for t in data.get("tax_history") or data.get("taxHistory") or []:
        if not t:
            continue
        a = t.get("assessment") or {}
        th.append({
            "property_id": pid,
            "year": _int(t.get("year")),
            "tax": _int(t.get("tax")),
            "assessed_total": _int(a.get("total")),
            "assessed_building": _int(a.get("building")),
            "assessed_land": _int(a.get("land")),
        })
    return {"price_history": ph, "tax_history": th}
