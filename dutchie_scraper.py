"""
dutchie_scraper.py
==================
Dutchie e-commerce scraper using the real internal GraphQL API.

Cloudflare Bypass:
    curl_cffi with impersonate="chrome120" defeats TLS fingerprinting.

Schema discovery: reverse-engineered from Dutchie's compiled app bundle
    (_app-93e16fa7303324d2.js) and confirmed via live API probing.

Dispensaries near Batavia, Ohio:
    - Shangri-La Amelia  → dispensaryId 69265b5d6fb4bb7d84f4a82a
    - Uplift Milford     → dispensaryId 633deac5361a0700d6564010

THC calculation for flower/concentrates:
    Ohio dispensaries store lab results in cannabinoidsV2.
    Total THC = THCA × 0.877 + THC-D9  (standard decarboxylation formula).
    Fallback: THCContent.range (already in % for those that have it).

Usage:
    1.  pip install curl_cffi requests
    2.  Set DISCORD_WEBHOOK_URL below (paste your webhook URL).
    3.  python dutchie_scraper.py
"""

# ─────────────────────────────────────────────────────────────────────────────
#  CONFIGURATION  –  edit before running
# ─────────────────────────────────────────────────────────────────────────────

import os

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

# Dispensaries: (MongoDB dispensaryId, friendly name)
DISPENSARIES = [
    ("69265b5d6fb4bb7d84f4a82a", "Shangri-La Amelia"),
    ("633deac5361a0700d6564010", "Uplift Milford"),
]

MAX_PRICE_DEAL   = 150.00    # inclusive upper bound ($) - increased to catch half oz/full oz
MIN_PRICE_DEAL   = 0.01      # must be > $0
MIN_THC_DEAL     = 18.0      # percent, inclusive

PER_PAGE         = 100       # Dutchie hard-caps perPage at 100 (larger values are silently clamped)
DISCORD_TOP_N    = 15        # Number of top deals per category
INTER_PAGE_DELAY = 0.3       # seconds between paginated requests

# Network / concurrency guards.  Dutchie occasionally stalls a request for 30s+
# with 0 bytes received, so every request and every store has a hard ceiling.
REQUEST_TIMEOUT  = 20        # seconds per GraphQL request
REQUEST_RETRIES  = 1         # extra attempts after a timeout / 5xx
STORE_DEADLINE   = 35        # seconds of wall-clock budget per store
MAX_WORKERS      = 5         # stores scraped in parallel
MAX_PAGES        = 40        # absolute safety cap (4,000 products)

# ─────────────────────────────────────────────────────────────────────────────

import io
import sys
import time
import re
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeout

# Force UTF-8 output so emoji/box-drawing chars work on any Windows console
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
else:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

try:
    from curl_cffi import requests as cf_requests
except ImportError:
    print("[FATAL] curl_cffi not installed.  Run:  pip install curl_cffi")
    sys.exit(1)

import requests as std_requests   # standard requests – only for Discord POST


# ══════════════════════════════════════════════════════════════════════════════
#  Real GraphQL query
#  Fields confirmed by: JS bundle analysis + live API introspection probing
# ══════════════════════════════════════════════════════════════════════════════

GRAPHQL_ENDPOINT = "https://dutchie.com/graphql"

FILTERED_PRODUCTS_QUERY = """
query FilteredProducts(
  $productsFilter: productsFilterInput!
  $useCache: Boolean
  $page: Int
  $perPage: Int
  $includeEnterpriseSpecials: Boolean = false
) {
  filteredProducts(
    filter: $productsFilter
    useCache: $useCache
    page: $page
    perPage: $perPage
    includeEnterpriseSpecials: $includeEnterpriseSpecials
  ) {
    products {
      _id
      id
      Name
      cName
      type
      strainType
      brandName
      Image
      Status
      Options
      Prices
      THC
      CBD
      THCContent { unit range }
      CBDContent { unit range }
      totalTerpenes { unit range }
      cannabinoidsV2 {
        value
        unit
        cannabinoid { name }
      }
    }
    queryInfo { totalCount }
  }
}
"""


def build_variables(dispensary_id: str, page: int) -> dict:
    """NOTE: `page` is 0-indexed.  An explicit sort is required — without it
    Dutchie returns overlapping pages and ~20% of the catalog is never seen."""
    return {
        "productsFilter": {
            "dispensaryId":                        dispensary_id,
            "Status":                              "Active",
            "removeProductsBelowOptionThresholds": False,
            "isKioskMenu":                         False,
            "bypassOnlineThresholds":              False,
            "sortBy":                              "name",
            "sortDirection":                       1,
        },
        "useCache":                 True,
        "page":                     page,
        "perPage":                  PER_PAGE,
        "includeEnterpriseSpecials": False,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  HTTP session – Chrome 120 TLS impersonation
# ══════════════════════════════════════════════════════════════════════════════

HEADERS = {
    "accept":                       "application/json",
    "accept-language":              "en-US,en;q=0.9",
    "apollographql-client-name":    "desktop-web",
    "apollographql-client-version": "1.0.0",
    "content-type":                 "application/json",
    "origin":                       "https://dutchie.com",
    "referer":                      "https://dutchie.com/",
    "sec-ch-ua":                    '"Chromium";v="120", "Google Chrome";v="120", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile":             "?0",
    "sec-ch-ua-platform":           '"Windows"',
    "sec-fetch-dest":               "empty",
    "sec-fetch-mode":               "cors",
    "sec-fetch-site":               "same-origin",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}


class ScrapeError(Exception):
    """Raised for unrecoverable per-request failures (timeouts, HTTP, GQL)."""


def post_graphql(session, variables: dict, timeout: float = REQUEST_TIMEOUT,
                 retries: int = REQUEST_RETRIES, raise_errors: bool = False,
                 deadline: float | None = None):
    """POST a FilteredProducts query with a hard timeout and bounded retries.

    `deadline` is an absolute time.monotonic() value; every attempt's timeout
    is clamped so the call can never run past it.

    Returns the parsed JSON dict, or None on failure (or raises ScrapeError if
    `raise_errors` is set, so callers can report *why* a store failed).
    """
    payload = {
        "operationName": "FilteredProducts",
        "query":         FILTERED_PRODUCTS_QUERY,
        "variables":     variables,
    }
    last_err = "unknown error"
    for attempt in range(retries + 1):
        attempt_timeout = float(timeout)
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining < 1.0:
                last_err = "store time budget exhausted"
                break
            attempt_timeout = min(attempt_timeout, remaining)
        try:
            resp = session.post(GRAPHQL_ENDPOINT, headers=HEADERS, json=payload,
                                timeout=attempt_timeout)
            if resp.status_code == 200:
                data = resp.json()
                if "errors" in data and not data.get("data"):
                    last_err = f"GraphQL error: {str(data['errors'])[:160]}"
                    break  # schema/validation errors won't fix themselves
                return data
            last_err = f"HTTP {resp.status_code}"
            if resp.status_code in (403, 429):
                last_err += " (blocked / rate-limited by Cloudflare)"
            if resp.status_code < 500 and resp.status_code != 429:
                break
        except Exception as exc:  # curl timeouts, connection resets, bad JSON
            msg = str(exc)
            last_err = "request timed out" if "timed out" in msg.lower() else msg[:160]
        if attempt < retries:
            time.sleep(0.75 * (attempt + 1))
    if raise_errors:
        raise ScrapeError(last_err)
    return None


# ══════════════════════════════════════════════════════════════════════════════
#  THC extraction – multi-source with proper unit handling
# ══════════════════════════════════════════════════════════════════════════════

def parse_thc_string(s) -> float | None:
    """
    Parse scalar 'THC' string: "22.4%", "18 - 24%", "22.4", None.
    Returns float (average for ranges) or None.
    """
    if not s or not isinstance(s, str):
        return None
    s = s.replace("%", "").strip()
    if s in ("", "N/A", "n/a", "NA"):
        return None
    for sep in [" - ", "-"]:
        if sep in s:
            parts = s.split(sep, 1)
            try:
                return round((float(parts[0].strip()) + float(parts[1].strip())) / 2, 2)
            except ValueError:
                pass
    try:
        v = float(s)
        return v if v > 0 else None
    except ValueError:
        return None


def parse_thc_content(obj) -> float | None:
    """
    Parse THCContent { unit, range }.
    Only returns a value when unit is PERCENTAGE (not MILLIGRAMS – edibles/beverages).
    This is the pre-computed lab THC%, trusted as-is.
    """
    if not obj:
        return None
    unit = (obj.get("unit") or "").upper()
    if unit not in ("PERCENTAGE", "%", "PERCENT", ""):
        return None   # skip mg values (edibles)
    rng = obj.get("range")
    if rng and isinstance(rng, list):
        vals = [float(v) for v in rng if v is not None]
        if vals:
            avg = round(sum(vals) / len(vals), 2)
            # Sanity check: THC% must be realistic (0-100%)
            if 0 < avg <= 100:
                return avg
    return None


def parse_cannabinoids_v2(cannabinoids: list) -> float | None:
    """
    Compute total THC from cannabinoidsV2 array.
    Total THC = THCA × 0.877 + THC-D9
    This matches the standard lab-reported "Total THC" figure.

    NOTE: Some Dutchie entries store mg values with a PERCENTAGE unit label
    (a data quality issue on their end). We reject values > 100 because
    no cannabis product has > 100% THC.
    """
    if not cannabinoids:
        return None

    thca  = 0.0
    d9thc = 0.0

    for c in cannabinoids:
        cb = c.get("cannabinoid") or {}
        name = (cb.get("name") or "").upper()
        unit = (c.get("unit") or "").upper()
        val  = c.get("value")
        if val is None:
            continue
        if unit not in ("PERCENTAGE", "%", "PERCENT", ""):
            continue  # skip confirmed mg values
        try:
            f = float(val)
        except (ValueError, TypeError):
            continue
        if f > 100:
            continue   # mislabeled mg value – skip

        if "THCA" in name:
            thca = max(thca, f)
        elif "THC-D9" in name or name in ("THC", "DELTA 9", "DELTA-9"):
            d9thc = max(d9thc, f)

    total = round(thca * 0.877 + d9thc, 2)
    # If total is still unrealistic after capping individual values, discard
    if total <= 0 or total > 100:
        return None
    return total


def best_thc(product: dict) -> float | None:
    """
    Try every known THC source.
    Priority:
      1. THCContent { unit: PERCENTAGE, range: [...] }  (pre-computed lab %)
         → Most reliable: Dutchie populates this from COA for OH dispensaries.
      2. cannabinoidsV2 (THCA×0.877 + THC-D9)
         → Full cannabinoid panel; we recalculate total THC ourselves.
      3. THC scalar string (rare in Ohio)
    """
    # Prefer pre-computed THCContent % over raw cannabinoid arithmetic
    thc = parse_thc_content(product.get("THCContent"))
    if thc is not None:
        return thc
    thc = parse_cannabinoids_v2(product.get("cannabinoidsV2") or [])
    if thc is not None:
        return thc
    thc = parse_thc_string(product.get("THC"))
    return thc


def best_cbd(product: dict) -> float | None:
    """Best-effort CBD % extraction using the same sources as THC."""
    cbd = parse_thc_content(product.get("CBDContent"))
    if cbd is not None:
        return cbd
    cbd = parse_thc_string(product.get("CBD"))
    return cbd


def parse_mg_string(s) -> float | None:
    """Parse a milligram value from a string like '100mg' or '100 mg'."""
    if not s or not isinstance(s, str):
        return None
    m = re.search(r"([\d\.]+)\s*mg", s.lower())
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


def best_total_thc_mg(product: dict) -> float | None:
    """Total THC in milligrams — the meaningful potency figure for edibles,
    tinctures, and beverages (which report potency in mg rather than %)."""
    obj = product.get("THCContent")
    if obj:
        unit = (obj.get("unit") or "").upper()
        if unit in ("MILLIGRAMS", "MG", "MILLIGRAM"):
            rng = obj.get("range")
            if rng and isinstance(rng, list):
                vals = [float(v) for v in rng if v is not None]
                if vals:
                    return round(sum(vals) / len(vals), 2)
    return parse_mg_string(product.get("THC"))


# ══════════════════════════════════════════════════════════════════════════════
#  Price helpers
# ══════════════════════════════════════════════════════════════════════════════

# Ohio menus label weight off a 28.3g ounce (the "Ohio tenth" is 2.83g, a half
# is listed as 14.15g, an ounce as 28.3g).  Eighths and quarters keep the
# universal retail convention of 3.5g / 7g that every menu actually prints.
OZ_GRAMS = 28.3
FRACTION_GRAMS = {
    (1, 10): 2.83,   # Ohio tenth
    (1, 8):  3.5,
    (1, 5):  5.66,   # Ohio two-tenths
    (1, 4):  7.0,
    (3, 10): 8.49,
    (2, 5):  11.32,
    (1, 2):  14.15,
    (3, 4):  21.23,
}
NAMED_WEIGHTS = {
    "tenth": 2.83, "eighth": 3.5, "quarter": 7.0, "half": 14.15, "ounce": OZ_GRAMS,
}


def _fraction_grams(num: float, den: float) -> float | None:
    if den == 0:
        return None
    key = (int(num), int(den)) if num.is_integer() and den.is_integer() else None
    if key in FRACTION_GRAMS:
        return FRACTION_GRAMS[key]
    return round(num / den * OZ_GRAMS, 2)


def parse_weight_in_grams(option_str: str) -> float | None:
    """Convert a Dutchie option/size label to grams.

    Handles: "1g", ".5g", "2.83g", "14.15g", "28.3g", "1/8 oz", "1/2oz",
    "1/8" (unit-less fraction == ounces), "1 oz", "0.5 oz", "Eighth",
    "5 x 0.5g", "0.5g x 5", "5pk .5g", "1g 2pk".
    Returns None for milligram (edible) labels or anything unparseable.
    """
    if not option_str:
        return None
    s = str(option_str).lower().strip().replace("½", "1/2").replace("¼", "1/4").replace("⅛", "1/8")
    if re.search(r"\d\s*mg\b", s) or s.endswith("mg"):
        return None

    num = r"(\d+(?:\.\d+)?|\.\d+)"

    # Multi-packs: "5 x 0.5g", "0.5g x 5", "5pk .5g", ".5g 5pk", "2-pack 1g"
    m = re.search(num + r"\s*(?:x|×)\s*" + num + r"\s*g\b", s)
    if m:
        return round(float(m.group(1)) * float(m.group(2)), 2)
    m = re.search(num + r"\s*g\s*(?:x|×)\s*(\d+)\b", s)
    if m:
        return round(float(m.group(1)) * float(m.group(2)), 2)
    m = re.search(r"(\d+)\s*-?\s*(?:pk|pack|ct)\b.*?" + num + r"\s*g\b", s)
    if m:
        return round(float(m.group(1)) * float(m.group(2)), 2)
    m = re.search(num + r"\s*g\b.*?(\d+)\s*-?\s*(?:pk|pack|ct)\b", s)
    if m:
        return round(float(m.group(1)) * float(m.group(2)), 2)

    # Fractional ounces, with or without "oz"
    m = re.search(r"(\d+)\s*/\s*(\d+)\s*(?:oz|ounce)?", s)
    if m:
        return _fraction_grams(float(m.group(1)), float(m.group(2)))

    # Decimal ounces: "1 oz", "0.5oz"
    m = re.search(num + r"\s*(?:oz|ounce)", s)
    if m:
        v = float(m.group(1))
        return 14.15 if v == 0.5 else round(v * OZ_GRAMS, 2)

    # Grams: "3.5g", ".5 g", "28.3 grams"
    m = re.search(num + r"\s*(?:g|gram|grams)\b", s)
    if m:
        v = float(m.group(1))
        return v if v > 0 else None

    for word, grams in NAMED_WEIGHTS.items():
        if word in s:
            return grams
    return None

def safe_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def get_prices_for_options(product: dict) -> dict:
    """
    Build {option_label: price} map from the parallel Options / Prices arrays.
    Prices is a [Float] array parallel to Options.
    """
    options = product.get("Options") or []
    prices  = product.get("Prices")  or []

    if not options:
        return {}

    if prices and len(prices) == len(options):
        result = {}
        for opt, pr in zip(options, prices):
            f = safe_float(pr)
            if f is not None and f > 0:
                result[str(opt)] = f
        return result

    return {}


# ══════════════════════════════════════════════════════════════════════════════
#  Core extraction – one product → one row per weight option
# ══════════════════════════════════════════════════════════════════════════════

# Categories whose potency is measured in mg and whose "weight" options (e.g.
# ".1g" on a chocolate bar) are not smokable grams — $/g is meaningless there.
NON_GRAM_CATEGORIES = ("edible", "tincture", "topical", "beverage", "capsule", "accessor", "apparel", "gear")


def build_product_url(store_slug: str, product: dict) -> str:
    """Dutchie product routes key off the product's cName slug
    (e.g. /dispensary/cascade-milford/product/gron-pomegranate-...).
    Fall back to the Mongo id only when no slug is published."""
    if not store_slug:
        return ""
    handle = product.get("cName") or product.get("id") or ""
    if not handle:
        return f"https://dutchie.com/dispensary/{store_slug}"
    return f"https://dutchie.com/dispensary/{store_slug}/product/{handle}"


def extract_rows(product: dict, store_name: str, store_slug: str = "") -> list:
    name      = product.get("Name") or "Unknown"
    category  = product.get("type") or "Unknown"
    strain    = product.get("strainType") or ""
    thc       = best_thc(product)
    options   = product.get("Options") or []
    price_map = get_prices_for_options(product)
    
    terps_obj = product.get("totalTerpenes") or {}
    terp_range = terps_obj.get("range") or []
    terpenes = terp_range[0] if terp_range else None

    cbd   = best_cbd(product)
    image = product.get("Image")
    brand = product.get("brandName")
    edible_mg = best_total_thc_mg(product)
    is_gram_product = not any(k in category.lower() for k in NON_GRAM_CATEGORIES)

    base = {
        "id":           product.get("id"),
        "product_slug": product.get("cName") or "",
        "product_url":  build_product_url(store_slug, product),
        "store_url":    f"https://dutchie.com/dispensary/{store_slug}" if store_slug else "",
        "slug":         store_slug,
        "store":        store_name,
        "name":         name,
        "category":     category,
        "strain":       strain,
        "brand":        brand,
        "image":        image,
        "thc":          thc,
        "cbd":          cbd,
        "terpenes":     terpenes,
    }

    rows = []

    if options:
        for opt in options:
            opt_str = str(opt)
            price   = price_map.get(opt_str)
            weight_g = parse_weight_in_grams(opt_str) if is_gram_product else None
            
            price_per_gram = round(price / weight_g, 2) if (weight_g and price) else None
            thc_per_dollar = round(thc / price, 2) if (thc and price) else None
            computed_mg = round(thc / 100.0 * weight_g * 1000.0, 1) if (thc and weight_g) else None
            opt_mg = parse_mg_string(opt_str)
            thc_mg_total = opt_mg or edible_mg or computed_mg
            thc_mg_per_dollar = round(thc_mg_total / price, 2) if (thc_mg_total and price) else None

            row = dict(base)
            row.update({
                "option":   opt_str,
                "price":    price,
                "weight_g": weight_g,
                "thc_mg_total": thc_mg_total,
                "price_per_gram": price_per_gram,
                "thc_per_dollar": thc_per_dollar,
                "thc_mg_per_dollar": thc_mg_per_dollar,
            })
            rows.append(row)
    else:
        row = dict(base)
        row.update({
            "option":   "",
            "price":    None,
            "weight_g": None,
            "thc_mg_total": edible_mg,
            "price_per_gram": None,
            "thc_per_dollar": None,
            "thc_mg_per_dollar": None,
        })
        rows.append(row)

    return rows


# ══════════════════════════════════════════════════════════════════════════════
#  Paginated dispensary scraper (deadline-bound, never hangs)
# ══════════════════════════════════════════════════════════════════════════════

def scrape_dispensary_detailed(session, dispensary_id: str, display_name: str,
                               store_slug: str = "",
                               deadline_s: float = STORE_DEADLINE) -> dict:
    """Scrape one store's full menu within a strict wall-clock budget.

    Returns {"name", "rows", "status", "error", "elapsed", "pages", "total"} where
    status is one of:
        ok       – full catalog retrieved
        partial  – budget ran out / a page failed after some data was retrieved
        timeout  – budget ran out before any data arrived
        error    – hard failure (HTTP / Cloudflare / GraphQL) with no data
    """
    start    = time.monotonic()
    deadline = start + deadline_s
    rows: list = []
    seen: set = set()
    page, total_count, pages_done = 0, None, 0
    status, error = "ok", None

    while True:
        if time.monotonic() >= deadline - 1.0:
            status, error = ("partial" if rows else "timeout"), f"exceeded {deadline_s:.0f}s budget"
            break
        if page >= MAX_PAGES:
            break
        try:
            data = post_graphql(session, build_variables(dispensary_id, page),
                                raise_errors=True, deadline=deadline)
        except ScrapeError as exc:
            msg = str(exc)
            timed_out = "timed out" in msg or "budget" in msg
            if rows:
                status = "partial"
            else:
                status = "timeout" if timed_out else "error"
            error = msg
            break

        fp       = (data.get("data") or {}).get("filteredProducts") or {}
        products = fp.get("products") or []
        if total_count is None:
            total_count = (fp.get("queryInfo") or {}).get("totalCount")
        pages_done += 1
        if not products:
            break

        new_ids = 0
        for product in products:
            pid = product.get("id") or product.get("_id")
            if pid in seen:
                continue
            seen.add(pid)
            new_ids += 1
            rows.extend(extract_rows(product, display_name, store_slug))

        # Termination guards: short page, no forward progress, or catalog complete
        if len(products) < PER_PAGE or new_ids == 0:
            break
        if total_count is not None and len(seen) >= total_count:
            break
        page += 1
        time.sleep(INTER_PAGE_DELAY)

    return {
        "name":    display_name,
        "rows":    rows,
        "status":  status,
        "error":   error,
        "elapsed": round(time.monotonic() - start, 1),
        "pages":   pages_done,
        "total":   total_count,
        "products": len(seen),
    }


def scrape_dispensary(session, dispensary_id: str, display_name: str, store_slug: str = "") -> list:
    """Backwards-compatible wrapper returning just the rows."""
    return scrape_dispensary_detailed(session, dispensary_id, display_name, store_slug)["rows"]


def _scrape_one(disp: dict, deadline_s: float) -> dict:
    session = cf_requests.Session(impersonate="chrome120")
    try:
        res = scrape_dispensary_detailed(session, disp["id"], disp.get("name") or disp["id"],
                                         disp.get("slug", ""), deadline_s)
    finally:
        try:
            session.close()
        except Exception:
            pass
    for r in res["rows"]:
        r["distance_mi"] = disp.get("distance_mi")
    res["dispensary"] = disp
    return res


def scrape_many(dispensaries: list, max_workers: int = MAX_WORKERS,
                per_store_timeout: float = STORE_DEADLINE, on_result=None) -> list:
    """Scrape many stores in parallel with full fault isolation.

    * Each store gets its own curl session and its own `per_store_timeout`
      budget enforced *inside* the worker (so threads always self-terminate).
    * An outer watchdog bounds the whole scan; any store that still hasn't
      reported is marked `timeout` and the scan returns immediately with the
      partial results from every store that did finish.
    * `on_result(result, done, total)` is invoked on the calling thread as each
      store completes — ideal for live UI progress.
    """
    if not dispensaries:
        return []
    workers = max(1, min(max_workers, len(dispensaries)))
    waves = -(-len(dispensaries) // workers)  # ceil
    overall_timeout = waves * (per_store_timeout + 5) + 10

    results: list = []
    ex = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="dutchie")
    futures = {ex.submit(_scrape_one, d, per_store_timeout): d for d in dispensaries}
    try:
        for fut in as_completed(futures, timeout=overall_timeout):
            d = futures[fut]
            try:
                res = fut.result()
            except Exception as exc:  # never let one store kill the scan
                res = {"name": d.get("name", d.get("id")), "rows": [], "status": "error",
                       "error": str(exc)[:200], "elapsed": None, "pages": 0,
                       "total": None, "products": 0, "dispensary": d}
            results.append(res)
            if on_result:
                on_result(res, len(results), len(dispensaries))
    except FuturesTimeout:
        for fut, d in futures.items():
            if not fut.done():
                fut.cancel()
                res = {"name": d.get("name", d.get("id")), "rows": [], "status": "timeout",
                       "error": "watchdog timeout", "elapsed": None, "pages": 0,
                       "total": None, "products": 0, "dispensary": d}
                results.append(res)
                if on_result:
                    on_result(res, len(results), len(dispensaries))
    finally:
        # Don't block on stragglers — they are deadline-bound and will exit.
        ex.shutdown(wait=False, cancel_futures=True)
    return results


# ══════════════════════════════════════════════════════════════════════════════
#  Deal filter
# ══════════════════════════════════════════════════════════════════════════════

def is_deal(row: dict) -> bool:
    price = row.get("price")
    thc   = row.get("thc")
    if price is None or thc is None:
        return False
    if not (MIN_PRICE_DEAL <= price <= MAX_PRICE_DEAL):
        return False
    if thc < MIN_THC_DEAL:
        return False
    return True


# ══════════════════════════════════════════════════════════════════════════════
#  Discord formatting & posting
# ══════════════════════════════════════════════════════════════════════════════

CATEGORY_EMOJI = {
    "flower":       "🌸",
    "vaporizers":   "💨",
    "concentrates": "🟡",
    "edibles":      "🍬",
    "other":        "🛒",
}

STORE_EMOJI = {
    "Shangri-La Amelia": "🏔️",
    "Uplift Milford":    "🌄",
}


def post_discord_deals(deals: list) -> None:
    if not DISCORD_WEBHOOK_URL or DISCORD_WEBHOOK_URL == "YOUR_DISCORD_WEBHOOK_URL_HERE":
        print("\n[INFO] No Discord webhook configured – skipping post.")
        print("[INFO] Edit DISCORD_WEBHOOK_URL at the top of the script to enable it.")
        return

    embed = {
        "title": "🚨 BEST DISPENSARY VALUE DEALS",
        "color": 3066993, # 0x2ECC71 Green
        "fields": []
    }
    
    top_deals = deals[:12]
    for i, deal in enumerate(top_deals, start=1):
        opt_str = deal["option"]
        opt_part = f" [{opt_str}]" if opt_str and opt_str.lower() not in deal["name"].lower() else ""
            
        thc_str = f"{deal['thc']:.1f}%" if deal['thc'] else "N/A"
        price_per_g_str = f" (${deal['price_per_gram']:.2f}/g)" if deal.get('price_per_gram') else ""
        
        # Formatting strain and terpenes
        strain_info = []
        if deal.get('strain'):
            strain_info.append(deal['strain'])
        if deal.get('terpenes'):
            strain_info.append(f"{deal['terpenes']:.2f}% Terps")
            
        strain_part = f" *({', '.join(strain_info)})*" if strain_info else ""
        
        name_str = f"**[{deal['store']}]** {deal['name']}{opt_part}{strain_part}"
        value_str = f"💰 **Price:** ${deal['price']:.2f}{price_per_g_str} | 🧪 **THC:** {thc_str} | 📦 **Category:** {deal['category']}"
        
        embed["fields"].append({
            "name": name_str,
            "value": value_str,
            "inline": False
        })
        
    if not embed["fields"]:
        return
        
    print(f"\n📤 Sending Discord webhook...")
    payload = {"embeds": [embed]}
    try:
        resp = std_requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)
        if resp.status_code in (200, 204):
            print(f"   ✔  Sent Discord Embed.")
        else:
            print(f"   ✗  Discord failed [{resp.status_code}]: {resp.text[:200]}")
    except Exception as exc:
        print(f"   ✗  Discord error: {exc}")


# ══════════════════════════════════════════════════════════════════════════════
#  Entry point
# ══════════════════════════════════════════════════════════════════════════════

def main():
    print("=" * 72)
    print("  Dutchie GraphQL Scraper  –  CF bypass via curl_cffi / Chrome120")
    print("  Stores: Shangri-La Amelia & Uplift Milford (Batavia, OH area)")
    print("=" * 72)

    session     = cf_requests.Session(impersonate="chrome120")
    all_rows    = []
    deals_found = []

    for dispensary_id, display_name in DISPENSARIES:
        rows = scrape_dispensary(session, dispensary_id, display_name)
        all_rows.extend(rows)
        for row in rows:
            if is_deal(row):
                deals_found.append(row)

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{'─'*72}")
    print(f"  Total option-rows extracted  : {len(all_rows):,}")
    print(f"  Deals matching criteria       : {len(deals_found)}")
    print(f"    Filter → Price ${MIN_PRICE_DEAL:.2f}–${MAX_PRICE_DEAL:.2f}, THC ≥ {MIN_THC_DEAL:.1f}%")
    print(f"{'─'*72}")

    if not deals_found:
        print("\n  No deals found matching the filter criteria.")
        print("  Note: THC data may not be populated for all products at all stores.")
        return

    # Multi-level sort: Price per Gram (ascending), then THC % (descending)
    # If price_per_gram is None, push it to the bottom
    deals_found.sort(
        key=lambda x: (
            x.get('price_per_gram') if x.get('price_per_gram') is not None else 999999.0, 
            -(x.get('thc') or 0.0)
        )
    )

    print(f"\n🏆  TOP 12 DEALS (Lowest Price/g -> Highest THC)")
    print("─" * 72)
    for i, deal in enumerate(deals_found[:12], 1):
        opt_str = deal["option"]
        opt_part = f" [{opt_str}]" if opt_str and opt_str.lower() not in deal["name"].lower() else ""
            
        strain_info = []
        if deal.get('strain'):
            strain_info.append(deal['strain'])
        if deal.get('terpenes'):
            strain_info.append(f"{deal['terpenes']:.2f}% Terps")
        strain_part = f" ({', '.join(strain_info)})" if strain_info else ""
        
        p_g_str = f" | ${deal['price_per_gram']:.2f}/g" if deal.get('price_per_gram') else ""
        thc_str = f"{deal['thc']:.1f}%" if deal.get('thc') else "N/A"
        
        print(
            f"  #{i:>2}  ${deal['price']:.2f}{p_g_str}  |  THC {thc_str}  |  "
            f"{deal['store']}  |  {deal['name']}{opt_part}{strain_part}"
        )

    post_discord_deals(deals_found)
    print("\n✅  Scan complete.")


if __name__ == "__main__":
    main()
