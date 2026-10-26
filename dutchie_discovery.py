"""
dutchie_discovery.py
=====================
Location-based dispensary discovery for the Dutchie Deal Finder.

Uses Dutchie's internal GraphQL `ConsumerDispensaries` query (reverse-engineered
from the compiled app bundle) to find dispensaries within a radius of a lat/lng,
and OpenStreetMap's Nominatim for free address/ZIP geocoding (no API key needed).

Lessons learned (verified against the live API):
  * A bare 5-digit ZIP sent to Nominatim's free-text search is ambiguous —
    "40031" resolves to a postcode in UKRAINE.  ZIPs are therefore looked up
    with `postalcode=` + `countrycodes=us`, backed by a hardcoded/persistent
    cache so a geocoding failure can never silently yield wrong coordinates.
  * `activeOnly: true` hides open, fully working stores (e.g. Shangri-La
    Amelia).  We instead request everything and keep `status == "open"`.
  * The API's `distance` filter/field is unreliable, so distance is computed
    client-side (haversine) from `location.geometry.coordinates`.
  * Dutchie does NOT clamp results to the query ZIP's state — a KY center
    returns OH rec + KY medical stores as long as the radius covers them.
"""

import json
import math
import os
import re
import time

from curl_cffi import requests as cf_requests

from dutchie_scraper import GRAPHQL_ENDPOINT, HEADERS

NOMINATIM_ENDPOINT = "https://nominatim.openstreetmap.org/search"
NOMINATIM_UA = "DutchieDealFinder/1.0 (personal-use price comparison tool)"

METERS_PER_MILE = 1609.344
RESOLVE_TIMEOUT = 10
DISCOVERY_TIMEOUT = 20

_HERE = os.path.dirname(os.path.abspath(__file__))
GEOCODE_CACHE_FILE = os.path.join(_HERE, "geocode_cache.json")

# Verified seed coordinates — used before any network call. Other ZIPs are
# resolved via US-scoped Nominatim and persisted to geocode_cache.json.
KNOWN_LOCATIONS = {
    "40031": (38.4065, -85.3788, "La Grange, KY 40031"),
}

# Slugs that Dutchie lists as "open" but are internal test / sandbox stores.
_JUNK_SLUG = re.compile(r"(^|-)(test\d*|sandbox|sbx|demo)(-|$)", re.I)

DISPENSARY_FIELDS = """
    id
    name
    cName
    status
    listImage
    recDispensary
    medicalDispensary
    offerPickup
    offerDelivery
    chain
    location {
      city
      state
      zipcode
      geometry { coordinates }
    }
"""

CONSUMER_DISPENSARIES_QUERY = (
    "query ConsumerDispensaries($dispensaryFilter: dispensariesFilterInput!) {\n"
    "  filteredDispensaries(filter: $dispensaryFilter) {" + DISPENSARY_FIELDS + "  }\n}"
)

# Verified Greater-Cincinnati + Northern-KY Dutchie stores (IDs confirmed live).
LOCAL_PRESETS = [
    {"id": "69265b5d6fb4bb7d84f4a82a", "name": "Shangri-La - Amelia",         "slug": "shangri-la-amelia",            "state": "OH"},
    {"id": "633deac5361a0700d6564010", "name": "UpLift - Milford",            "slug": "cascade-milford",              "state": "OH"},
    {"id": "633deaa469a61b00a343785f", "name": "UpLift - Mt. Orab",           "slug": "cascade-mt-orab",              "state": "OH"},
    {"id": "6a5529646151fd0a3633dc7c", "name": "Verilife - Ridge Avenue OH",  "slug": "verilife-cincinnati-oh",       "state": "OH"},
    {"id": "682cf4d00c514098de0fae88", "name": "The Garden Sycamore",         "slug": "sycamore-township-cincinnati", "state": "OH"},
    {"id": "66f6ffed0c40fe56725605ec", "name": "Ascend - Cincinnati",         "slug": "ascend-cincinnati-ohio",       "state": "OH"},
    {"id": "690e8be7b1ec367ee8ce0287", "name": "Zips - Central Parkway",      "slug": "n2b-ohio",                     "state": "OH"},
    {"id": "682353fc60478e6199264468", "name": "Locals Dispensary Cincinnati","slug": "locals-dispensary-cincinnati", "state": "OH"},
    {"id": "682cf3e4553a5f4e2ec3be70", "name": "The Garden Camp Washington",  "slug": "camp-washington-cincinnati",   "state": "OH"},
    {"id": "67e31db7725e304d9e0af23d", "name": "Therapy - Cincinnati",        "slug": "therapy-cincinnati",           "state": "OH"},
    {"id": "69e67243f3d1aaa3fcd109de", "name": "Yellow Flowers - Erlanger",   "slug": "yellow-flowers-erlanger",      "state": "KY"},
]


# ══════════════════════════════════════════════════════════════════════════════
#  Geocoding
# ══════════════════════════════════════════════════════════════════════════════

def _load_geo_cache() -> dict:
    try:
        with open(GEOCODE_CACHE_FILE, "r", encoding="utf-8") as f:
            return {k: tuple(v) for k, v in json.load(f).items()}
    except Exception:
        return {}


def _save_geo_cache(cache: dict) -> None:
    try:
        with open(GEOCODE_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=1)
    except Exception:
        pass


_GEO_CACHE = _load_geo_cache()


def _valid_us_coords(lat, lng) -> bool:
    # Continental US + AK/HI bounding box; rejects (0,0) and foreign hits.
    return lat is not None and lng is not None and 18.0 <= lat <= 72.0 and -180.0 <= lng <= -65.0


def geocode(query, session=None):
    """Resolve a US ZIP / address / "lat, lng" string to (lat, lng, label).

    Never returns (0, 0) or a foreign location: returns (None, None, None)
    if nothing trustworthy is found.
    """
    q = (query or "").strip()
    if not q:
        return None, None, None

    m = re.match(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$", q)
    if m:
        lat, lng = float(m.group(1)), float(m.group(2))
        return lat, lng, f"{lat:.4f}, {lng:.4f}"

    zip_m = re.match(r"^(\d{5})(?:-\d{4})?$", q)
    key = zip_m.group(1) if zip_m else q.lower()

    if key in KNOWN_LOCATIONS:
        return KNOWN_LOCATIONS[key]
    if key in _GEO_CACHE:
        return _GEO_CACHE[key]

    # Optional offline ZIP database if the user has `pgeocode` installed.
    if zip_m:
        try:
            import pgeocode  # type: ignore
            rec = pgeocode.Nominatim("us").query_postal_code(key)
            if rec is not None and _valid_us_coords(rec.latitude, rec.longitude):
                result = (float(rec.latitude), float(rec.longitude),
                          f"{rec.place_name}, {rec.state_code} {key}")
                _GEO_CACHE[key] = result
                _save_geo_cache(_GEO_CACHE)
                return result
        except Exception:
            pass

    if session is None:
        session = cf_requests.Session(impersonate="chrome120")
    params = {"format": "json", "limit": 1, "countrycodes": "us", "addressdetails": 1}
    if zip_m:
        params["postalcode"] = key
    else:
        params["q"] = q
    try:
        time.sleep(1.0)  # Nominatim usage policy: max 1 req/sec
        resp = session.get(NOMINATIM_ENDPOINT, params=params,
                           headers={"User-Agent": NOMINATIM_UA, "Accept": "application/json"},
                           timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if data:
                lat, lng = float(data[0]["lat"]), float(data[0]["lon"])
                if _valid_us_coords(lat, lng):
                    addr = data[0].get("address") or {}
                    town = addr.get("city") or addr.get("town") or addr.get("village") or ""
                    state = addr.get("state") or ""
                    label = f"{town}, {state} {key}".strip(", ") if zip_m and town else data[0].get("display_name", q)
                    result = (lat, lng, label)
                    _GEO_CACHE[key] = result
                    _save_geo_cache(_GEO_CACHE)
                    return result
    except Exception:
        pass
    return None, None, None


def haversine_miles(lat1, lng1, lat2, lng2) -> float:
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# ══════════════════════════════════════════════════════════════════════════════
#  Dispensary lookup
# ══════════════════════════════════════════════════════════════════════════════

def _gql_dispensaries(session, flt: dict, timeout: float):
    payload = {
        "operationName": "ConsumerDispensaries",
        "query": CONSUMER_DISPENSARIES_QUERY,
        "variables": {"dispensaryFilter": flt},
    }
    resp = session.post(GRAPHQL_ENDPOINT, headers=HEADERS, json=payload, timeout=timeout)
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}")
    data = resp.json()
    if "errors" in data and not data.get("data"):
        raise RuntimeError(str(data["errors"])[:200])
    return ((data.get("data") or {}).get("filteredDispensaries")) or []


def _normalize(it: dict, origin=None) -> dict:
    loc = it.get("location") or {}
    coords = (loc.get("geometry") or {}).get("coordinates") or []
    lat = lng = None
    if len(coords) == 2 and coords[0] is not None:
        lng, lat = float(coords[0]), float(coords[1])
    dist = None
    if origin and lat is not None:
        dist = round(haversine_miles(origin[0], origin[1], lat, lng), 1)
    return {
        "id": it.get("id"),
        "name": it.get("name") or it.get("cName"),
        "slug": it.get("cName"),
        "distance_mi": dist,
        "lat": lat,
        "lng": lng,
        "image": it.get("listImage"),
        "city": loc.get("city"),
        "state": loc.get("state"),
        "zipcode": loc.get("zipcode"),
        "rec": bool(it.get("recDispensary")),
        "medical": bool(it.get("medicalDispensary")),
        "offer_pickup": bool(it.get("offerPickup")),
        "offer_delivery": bool(it.get("offerDelivery")),
        "chain": it.get("chain"),
        "status": it.get("status"),
    }


def discover_dispensaries(session, lat, lng, radius_miles=30.0, limit=150, include_medical=True):
    """Return open dispensaries within `radius_miles` (true great-circle
    distance) of (lat, lng), across state lines, rec + medical, sorted by
    distance.  Raises on network failure so the UI can report it."""
    flt = {
        "nearLat": lat,
        "nearLng": lng,
        # Ask wide; the server's distance filter is approximate. Clamp below.
        "distance": int((radius_miles + 25) * METERS_PER_MILE),
        "sortBy": "distance",
    }
    items = _gql_dispensaries(session, flt, DISCOVERY_TIMEOUT)

    out, seen = [], set()
    for it in items:
        if (it.get("status") or "").lower() != "open":
            continue
        slug = it.get("cName") or ""
        if not it.get("id") or it["id"] in seen or _JUNK_SLUG.search(slug):
            continue
        d = _normalize(it, (lat, lng))
        if d["distance_mi"] is None or d["distance_mi"] > radius_miles:
            continue
        if not include_medical and not d["rec"]:
            continue
        seen.add(it["id"])
        out.append(d)
    out.sort(key=lambda d: d["distance_mi"])
    return out[:limit]


def extract_slug(slug_or_url: str) -> str | None:
    """Accept any Dutchie URL format or a bare slug and return the store slug.

    Supported:  https://dutchie.com/dispensary/<slug>[/...]
                https://dutchie.com/embedded-menu/<slug>[/...]
                https://dutchie.com/stores/<slug>[/...]
                dutchie.com/dispensary/<slug>?menuType=rec
                <slug>   (or a raw 24-char dispensary ID)
    """
    s = (slug_or_url or "").strip()
    if not s:
        return None
    m = re.search(r"/(?:dispensary|dispensaries|embedded-menu|stores|store)/([A-Za-z0-9_-]+)", s)
    if m:
        return m.group(1).lower()
    if "dutchie.com" in s or "/" in s:
        return None
    s = s.split("?")[0].strip().lower()
    return s if re.fullmatch(r"[a-z0-9_-]+", s) else None


def resolve_dispensary(slug_or_url, session=None, origin=None):
    """Resolve a slug / URL / ID to a full dispensary dict (or None).
    Strict 10-second budget.  Uses the GraphQL `cNameOrID` filter first and
    falls back to scraping the store page HTML for the dispensaryId."""
    slug = extract_slug(slug_or_url)
    if not slug:
        return None
    if session is None:
        session = cf_requests.Session(impersonate="chrome120")
    start = time.monotonic()
    try:
        items = _gql_dispensaries(session, {"cNameOrID": slug}, RESOLVE_TIMEOUT)
        if items:
            return _normalize(items[0], origin)
    except Exception:
        pass
    remaining = RESOLVE_TIMEOUT - (time.monotonic() - start)
    if remaining < 1:
        return None
    try:
        r = session.get(f"https://dutchie.com/dispensary/{slug}", timeout=remaining)
        ids = re.findall(r'"dispensaryId":"([a-f0-9]{24})"', r.text)
        if ids:
            return {"id": ids[0], "name": slug.replace("-", " ").title(), "slug": slug, "distance_mi": None}
    except Exception:
        pass
    return None


def with_distances(dispensaries: list, origin) -> list:
    """Recompute distance_mi for stores that carry lat/lng."""
    if not origin:
        return dispensaries
    for d in dispensaries:
        if d.get("lat") is not None and d.get("lng") is not None:
            d["distance_mi"] = round(haversine_miles(origin[0], origin[1], d["lat"], d["lng"]), 1)
    return dispensaries
