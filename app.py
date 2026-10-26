"""
Dutchie Deal Finder — Streamlit UI
===================================
Discover dispensaries near you, scan their live menus, and surface the
absolute best value deals (lowest $/g, highest THC-per-dollar).

Run:  streamlit run app.py
"""

import html
import json
import os
import re
import time

import pandas as pd
import streamlit as st
from curl_cffi import requests as cf_requests

from dutchie_scraper import scrape_many, STORE_DEADLINE, MAX_WORKERS
from dutchie_discovery import (
    LOCAL_PRESETS,
    discover_dispensaries,
    geocode,
    resolve_dispensary,
    with_distances,
)

CONFIG_FILE = "dutchie_config.json"

DEFAULTS = {
    "dispensaries": [],
    "home_location": "",
    "radius_miles": 30,
    "price_range": [1.0, 150.0],
    "thc_range": [0.0, 100.0],
    "strains": ["Sativa", "Indica", "Hybrid"],
    "categories": "Flower",
    "discord_webhook": "",
}

ALL_STRAINS = ["Sativa", "Indica", "Hybrid"]
ALL_CATEGORIES = ["Flower", "Pre-Rolls", "Vaporizers", "Concentrates", "Edibles", "Other"]

CATEGORY_EMOJI = {
    "Flower": "🌸",
    "Pre-Rolls": "🚬",
    "Vaporizers": "💨",
    "Concentrates": "🍯",
    "Edibles": "🍬",
    "Other": "🌿",
}

# Weight / size filter buckets, keyed by mapped category.
#   - Flower / Pre-Rolls / Vaporizers / Concentrates → grams (from `weight_g`)
#   - Edibles → total THC milligrams (from `thc_mg_total`)
# Each entry is (label, min_value, max_value); a deal matches if its value is
# within the inclusive [min, max] range.
WEIGHT_OPTIONS = {
    "Flower": [
        # Ranges are the midpoints between neighboring sizes, so any weight
        # rounds to the closest bucket: 2.83→3.5, 3.54→3.5, 5.66→5.66,
        # 8.49→7, 11.32→14, 14.15→14, 28.3→28.
        ("1g", 0.0, 2.25),
        ("3.5g", 2.25, 4.58),
        ("5.66g", 4.58, 6.33),
        ("7g", 6.33, 10.5),
        ("14g", 10.5, 21.0),
        ("28g", 21.0, 9999.0),
    ],
    "Pre-Rolls": [
        ("0.5g", 0.3, 0.7),
        ("1g", 0.8, 1.9),
        ("2g", 1.9, 3.0),
        ("5g", 4.5, 6.0),
        ("10g", 9.0, 11.0),
    ],
    "Vaporizers": [
        ("0.3g", 0.2, 0.4),
        ("0.5g", 0.4, 0.7),
        ("1g", 0.8, 1.5),
        ("2g", 1.5, 2.5),
    ],
    "Concentrates": [
        ("0.5g", 0.3, 0.7),
        ("1g", 0.8, 1.5),
        ("2g", 1.5, 2.5),
        ("3.5g", 3.0, 4.0),
    ],
    "Edibles": [
        ("10mg", 5, 25),
        ("50mg", 40, 75),
        ("100mg", 80, 200),
        ("500mg", 400, 750),
        ("1000mg", 800, 1500),
    ],
    "Other": [],
}


def weight_value_for(deal, category):
    """Numeric 'size' value for a deal, in the unit relevant to its category."""
    if category == "Edibles":
        return deal.get("thc_mg_total")
    return deal.get("weight_g")


def deal_matches_any_weight(deal, category, labels):
    if not labels:
        return True
    buckets = WEIGHT_OPTIONS.get(category, [])
    v = weight_value_for(deal, category)
    if v is None:
        return False
    for lbl, lo, hi in buckets:
        if lbl in labels and lo <= v <= hi:
            return True
    return False


PRICE_BOUNDS = (1.0, 300.0)
THC_BOUNDS = (0.0, 100.0)
RADIUS_BOUNDS = (5, 100)


def _clamp_range(value, bounds, default):
    """Coerce anything into a sorted [lo, hi] pair within slider bounds."""
    try:
        lo, hi = float(value[0]), float(value[1])
    except Exception:
        return list(default)
    lo, hi = sorted((lo, hi))
    lo = min(max(lo, bounds[0]), bounds[1])
    hi = min(max(hi, bounds[0]), bounds[1])
    return [lo, hi]


def _valid_dispensaries(value):
    out, seen = [], set()
    for d in value if isinstance(value, list) else []:
        if isinstance(d, dict) and d.get("id") and d["id"] not in seen:
            seen.add(d["id"])
            d.setdefault("name", d.get("slug") or d["id"])
            d.setdefault("slug", "")
            out.append(d)
    return out


def validate_config(cfg):
    """Return a fully-populated config with every key type- and bounds-checked."""
    clean = dict(DEFAULTS)
    clean["dispensaries"] = _valid_dispensaries(cfg.get("dispensaries"))
    clean["home_location"] = str(cfg.get("home_location") or "")
    try:
        r = int(cfg.get("radius_miles", DEFAULTS["radius_miles"]))
    except Exception:
        r = DEFAULTS["radius_miles"]
    clean["radius_miles"] = min(max(r, RADIUS_BOUNDS[0]), RADIUS_BOUNDS[1])
    clean["price_range"] = _clamp_range(cfg.get("price_range"), PRICE_BOUNDS, DEFAULTS["price_range"])
    clean["thc_range"] = _clamp_range(cfg.get("thc_range"), THC_BOUNDS, DEFAULTS["thc_range"])
    strains = cfg.get("strains")
    clean["strains"] = [s for s in strains if s in ALL_STRAINS] if isinstance(strains, list) else list(ALL_STRAINS)
    cats = cfg.get("categories")
    if isinstance(cats, list):  # legacy multi-select configs
        cats = cats[0] if cats else None
    clean["categories"] = cats if cats in ALL_CATEGORIES else None
    clean["discord_webhook"] = str(cfg.get("discord_webhook") or "")
    return clean


def load_config():
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                if "min_price" in loaded and "max_price" in loaded:
                    cfg["price_range"] = [loaded["min_price"], loaded["max_price"]]
                if "min_thc" in loaded and "max_thc" in loaded:
                    cfg["thc_range"] = [loaded["min_thc"], loaded["max_thc"]]
                for k, v in loaded.items():
                    if k in cfg and k not in ("min_price", "max_price", "min_thc", "max_thc"):
                        cfg[k] = v
        except Exception:
            pass
    return validate_config(cfg)


def save_config():
    cfg = {
        "dispensaries": st.session_state.get("dispensaries", []),
        "home_location": st.session_state.get("home_location", ""),
        "radius_miles": st.session_state.get("radius_miles", 30),
        "price_range": st.session_state.get("price_range", [1.0, 150.0]),
        "thc_range": st.session_state.get("thc_range", [0.0, 100.0]),
        "strains": st.session_state.get("strains") or [],
        "categories": st.session_state.get("categories"),
        "discord_webhook": st.session_state.get("discord_webhook", ""),
    }
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


# ══════════════════════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════════════════════

def map_category(cat_raw):
    c = (cat_raw or "").lower()
    if "pre-roll" in c or "preroll" in c:
        return "Pre-Rolls"
    if "flower" in c or "bud" in c or "shake" in c or "trim" in c:
        return "Flower"
    if "vapor" in c or "cart" in c or "pod" in c or "pen" in c or "dab" in c or "disposable" in c:
        return "Vaporizers"
    if "concentrate" in c or "extract" in c or "resin" in c or "rosin" in c or "wax" in c or "shatter" in c or "crumble" in c or "hash" in c:
        return "Concentrates"
    if "edible" in c or "tincture" in c or "beverage" in c or "gumm" in c or "drink" in c or "capsule" in c or "chocolate" in c:
        return "Edibles"
    return "Other"


def strain_class(strain):
    s = (strain or "").lower()
    if "sativa" in s:
        return "sativa"
    if "indica" in s:
        return "indica"
    if "hybrid" in s:
        return "hybrid"
    return "category"


def fmt_money(v):
    return f"${v:,.2f}" if v is not None else "—"


def compute_deal_scores(deals):
    mgs = [d.get("thc_mg_per_dollar") for d in deals if d.get("thc_mg_per_dollar")]
    ppgs = [d.get("price_per_gram") for d in deals if d.get("price_per_gram")]
    mg_best = max(mgs) if mgs else None
    ppg_best = min(ppgs) if ppgs else None
    for d in deals:
        mg = d.get("thc_mg_per_dollar")
        if mg and mg_best:
            d["deal_score"] = round(min(100.0, mg / mg_best * 100.0))
        elif d.get("price_per_gram") and ppg_best:
            d["deal_score"] = round(min(100.0, ppg_best / d["price_per_gram"] * 100.0))
        else:
            d["deal_score"] = 0
    return deals


def add_savings(deals):
    df = pd.DataFrame(deals)
    if df.empty or "mapped_cat" not in df.columns:
        for d in deals:
            d["savings"] = 0.0
        return deals
    medians = df.groupby("mapped_cat")["price"].median().to_dict()
    for d in deals:
        m = medians.get(d.get("mapped_cat"))
        d["savings"] = round(m - d["price"], 2) if (m is not None and d.get("price") is not None) else 0.0
    return deals


def sort_deals(deals, key="value"):
    if key == "value":
        return sorted(deals, key=lambda d: (-(d.get("thc_mg_per_dollar") or 0.0), d.get("price_per_gram") if d.get("price_per_gram") else 999999.0, -(d.get("thc") or 0.0)))
    if key == "price_per_gram":
        return sorted(deals, key=lambda d: d.get("price_per_gram") if d.get("price_per_gram") else 999999.0)
    if key == "thc":
        return sorted(deals, key=lambda d: -(d.get("thc") or 0.0))
    if key == "price":
        return sorted(deals, key=lambda d: d.get("price") if d.get("price") else 999999.0)
    if key == "distance":
        return sorted(deals, key=lambda d: d.get("distance_mi") if d.get("distance_mi") is not None else 999999.0)
    if key == "savings":
        return sorted(deals, key=lambda d: -(d.get("savings") or 0.0))
    return deals
def filter_rows(rows, price_range, thc_range, strains, categories, weight_labels=None):
    p_min, p_max = price_range
    t_min, t_max = thc_range
    if isinstance(categories, str):
        categories = [categories] if categories else []
    elif categories is None:
        categories = []
    out = []
    for r in rows:
        price = r.get("price")
        if price is None:
            continue
        if not (p_min <= price <= p_max):
            continue
        mapped = map_category(r.get("category"))
        thc = r.get("thc")
        # THC % only applies to potency-percentage products. Edibles report
        # potency in total mg (thc_mg_total), so they skip the % filter.
        if mapped != "Edibles":
            if thc is None or not (t_min <= thc <= t_max):
                continue
        if strains:
            strain_low = (r.get("strain") or "").lower()
            if not any(s.lower() in strain_low for s in strains):
                continue
        if categories and mapped not in categories:
            continue
        r["mapped_cat"] = mapped
        if weight_labels and not deal_matches_any_weight(r, mapped, weight_labels):
            continue
        out.append(r)
    return out


@st.cache_data(ttl=86400, show_spinner=False)
def cached_geocode(query):
    session = cf_requests.Session(impersonate="chrome120")
    return geocode(query, session)


@st.cache_data(ttl=3600, show_spinner=False)
def cached_discover(lat, lng, radius_miles):
    session = cf_requests.Session(impersonate="chrome120")
    return discover_dispensaries(session, lat, lng, radius_miles)


st.set_page_config(page_title="Dutchie Deal Finder", page_icon="🌿", layout="wide", initial_sidebar_state="expanded")

config = load_config()
for k, v in config.items():
    if k not in st.session_state:
        st.session_state[k] = v
# ══════════════════════════════════════════════════════════════════════════════
#  Custom CSS
# ══════════════════════════════════════════════════════════════════════════════
CUSTOM_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Sora:wght@500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap');

:root {
  --bg: #0a0d13;
  --surface: #151b27;
  --surface-2: #1b2231;
  --border: rgba(255,255,255,0.08);
  --text: #e7ecf3;
  --muted: #8a94a6;
  --accent: #10b981;
  --accent-2: #2dd4bf;
  --gold: #f5c542;
  --indica: #c084fc;
  --sativa: #facc15;
  --hybrid: #38bdf8;
}

html, body, [class*="st-"]:not(i):not(.material-symbols-rounded) { font-family: 'Inter', sans-serif; }
.mono { font-family: 'JetBrains Mono', monospace; }

section[data-testid="stSidebar"] { background: #0d121b; border-right: 1px solid var(--border); }

.hero-banner {
  background: linear-gradient(120deg, rgba(16,185,129,0.14), rgba(45,212,191,0.06) 45%, rgba(245,197,66,0.07));
  border: 1px solid var(--border);
  border-radius: 18px;
  padding: 26px 30px;
  margin-bottom: 6px;
}
.hero-banner h1 { margin: 0; font-family: 'Sora', sans-serif; font-weight: 700; letter-spacing: -0.02em; color: #fff; font-size: 30px; }
.hero-banner p { color: var(--muted); margin: 6px 0 0; font-size: 15px; }

.kpi-row { display: flex; gap: 14px; flex-wrap: wrap; margin: 18px 0 8px; }
.kpi-card {
  flex: 1 1 170px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 14px;
  padding: 16px 18px;
  min-width: 150px;
}
.kpi-label { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: 0.06em; }
.kpi-value { font-size: 26px; font-weight: 600; color: var(--text); margin-top: 6px; font-family: 'JetBrains Mono', monospace; }
.kpi-sub { color: var(--muted); font-size: 12px; margin-top: 3px; }

.section-title { font-family: 'Sora', sans-serif; font-weight: 600; color: var(--text); font-size: 18px; margin: 22px 0 12px; }

.deal-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 16px;
  overflow: hidden;
  transition: transform .15s ease, border-color .15s ease, box-shadow .15s ease;
  height: 100%;
  display: flex;
  flex-direction: column;
}
.deal-card:hover { transform: translateY(-3px); border-color: rgba(16,185,129,0.5); box-shadow: 0 12px 32px rgba(0,0,0,0.45); }
.deal-imgwrap { position: relative; height: 128px; background: linear-gradient(120deg,#1b2231,#151b27); }
.deal-img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; display: block; }
.deal-img-fallback { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; }
.fallback-emoji { font-size: 42px; opacity: 0.45; }
.store-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--accent); display: inline-block; margin-right: 6px; vertical-align: middle; }
.deal-card summary { list-style: none; cursor: pointer; display: flex; flex-direction: column; flex: 1; outline: none; color: inherit; }
.deal-card summary::-webkit-details-marker { display: none; }
.deal-card summary::marker { display: none; content: ''; }
.deal-hint { text-align: center; padding: 8px 12px; font-size: 11px; font-weight: 600; color: var(--muted); border-top: 1px solid var(--border); transition: color .15s ease, background .15s ease; }
.deal-card:hover .deal-hint { color: var(--accent); background: rgba(16,185,129,0.05); }
details[open] .hint-more { display: none; }
details:not([open]) .hint-less { display: none; }
.deal-details { padding: 14px 15px 6px; border-top: 1px solid var(--border); display: flex; flex-direction: column; gap: 8px; background: rgba(255,255,255,0.02); }
.detail-row { display: flex; justify-content: space-between; gap: 12px; font-size: 12.5px; line-height: 1.4; }
.detail-row .k { color: var(--muted); flex: 0 0 auto; }
.detail-row .v { color: var(--text); text-align: right; font-weight: 500; }
.deal-links { display: flex; gap: 8px; padding: 12px 15px 15px; border-top: 1px solid var(--border); }
.deal-btn { flex: 1; text-align: center; padding: 9px 10px; font-size: 12.5px; font-weight: 600; border-radius: 9px; text-decoration: none !important; color: var(--accent); background: rgba(16,185,129,0.08); border: 1px solid rgba(16,185,129,0.3); transition: background .15s ease, color .15s ease; }
.deal-btn:hover { background: rgba(16,185,129,0.18); color: #fff; }
.deal-btn-primary { background: linear-gradient(120deg,#10b981,#2dd4bf); color: #04281f; border: none; }
.deal-btn-primary:hover { color: #04281f; background: linear-gradient(120deg,#2dd4bf,#10b981); }
.deal-btn-disabled { color: var(--muted); background: var(--surface-2); border-color: var(--border); pointer-events: none; }
.rank-badge {
  position: absolute; top: 10px; left: 10px;
  background: rgba(10,13,19,0.85); color: var(--gold);
  padding: 3px 9px; border-radius: 999px; font-size: 11px; font-weight: 700;
  border: 1px solid rgba(245,197,66,0.4);
}
.deal-body { padding: 13px 15px 15px; display: flex; flex-direction: column; gap: 9px; flex: 1; }
.deal-store { color: var(--muted); font-size: 12px; display: flex; align-items: center; gap: 5px; }
.deal-name { font-weight: 600; color: #fff; font-size: 14px; line-height: 1.35; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; min-height: 38px; }
.deal-sub { color: var(--muted); font-size: 12px; font-family: 'JetBrains Mono', monospace; }
.deal-metrics { display: flex; align-items: baseline; gap: 10px; margin-top: auto; flex-wrap: wrap; }
.deal-price { font-size: 20px; font-weight: 600; color: var(--accent); font-family: 'JetBrains Mono', monospace; }

.badge { display: inline-block; padding: 3px 9px; border-radius: 6px; font-size: 11px; font-weight: 500; margin-right: 3px; }
.badge.sativa { background: rgba(250,204,21,0.12); color: var(--sativa); }
.badge.indica { background: rgba(192,132,252,0.12); color: var(--indica); }
.badge.hybrid { background: rgba(56,189,248,0.12); color: var(--hybrid); }
.badge.category { background: var(--surface-2); color: var(--muted); }
.badge.score { background: rgba(16,185,129,0.12); color: var(--accent); }
.badge.save { background: rgba(245,197,66,0.12); color: var(--gold); }

.stButton > button { border-radius: 10px; font-weight: 600; }
.stButton > button[kind="primary"] { background: linear-gradient(120deg,#10b981,#2dd4bf); color: #04281f; border: none; }

.stTabs [data-baseweb="tab-list"] { gap: 4px; }
.stTabs [data-baseweb="tab"] { padding: 8px 14px; border-radius: 10px; color: var(--muted); }
.stTabs [aria-selected="true"] { color: var(--accent); }

[data-testid="stDataFrame"] { border: 1px solid var(--border); border-radius: 12px; }
</style>
"""
st.markdown(CUSTOM_CSS, unsafe_allow_html=True)
# ══════════════════════════════════════════════════════════════════════════════
#  Icons
# ══════════════════════════════════════════════════════════════════════════════
def get_icon(name):
    icons = {
        "store": '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"></path><polyline points="9 22 9 12 15 12 15 22"></polyline></svg>',
        "pin": '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z"></path><circle cx="12" cy="10" r="3"></circle></svg>',
        "spark": '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2l2.4 7.6L22 12l-7.6 2.4L12 22l-2.4-7.6L2 12l7.6-2.4z"></path></svg>',
        "x": '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>',
        "activity": '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#10b981" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"></polyline></svg>',
        }
    return icons.get(name, "")


# ══════════════════════════════════════════════════════════════════════════════
#  Sidebar
# ══════════════════════════════════════════════════════════════════════════════
with st.sidebar:
    st.markdown("<h2 style='font-family:Sora,sans-serif;margin:0;color:#fff;'>🌿 Dutchie Deals</h2>", unsafe_allow_html=True)
    st.markdown("<span style='color:#8a94a6;font-size:12px;'>Find the best value in your area</span>", unsafe_allow_html=True)
    st.markdown("<br>", unsafe_allow_html=True)

    st.markdown("**📍 Your location**")
    st.text_input("ZIP or address", key="home_location", placeholder="e.g. 45103 or Cincinnati, OH", label_visibility="collapsed", on_change=save_config)
    st.slider("Search radius (miles)", 5, 100, key="radius_miles", on_change=save_config)

    if st.button("🔭 Discover nearby dispensaries", use_container_width=True, type="primary"):
        loc = st.session_state.get("home_location", "")
        if not loc:
            st.error("Enter a ZIP code or address first.")
        else:
            lat, lng, label = cached_geocode(loc)
            if lat is None:
                st.error(f"Couldn't geocode “{loc}”. Try a ZIP code or “City, ST”.")
            else:
                with st.spinner(f"Searching near {label}…"):
                    found = cached_discover(lat, lng, st.session_state.get("radius_miles", 30))
                if not found:
                    st.warning("No dispensaries found in that radius.")
                else:
                    st.session_state["dispensaries"] = found
                    save_config()
                    st.success(f"Found {len(found)} dispensaries within {st.session_state['radius_miles']} mi.")
                    st.rerun()

    st.markdown("**🏬 Dispensaries**")
    add_input = st.text_input("Add by slug or URL", placeholder="e.g. shangri-la-amelia", label_visibility="collapsed")
    col_add1, col_add2 = st.columns([1, 1])
    with col_add1:
        if st.button("➕ Add store", use_container_width=True):
            if add_input:
                loc = st.session_state.get("home_location", "")
                lat, lng, _ = cached_geocode(loc) if loc else (None, None, None)
                session = cf_requests.Session(impersonate="chrome120")
                disp = resolve_dispensary(add_input, session=session, origin=(lat, lng) if lat else None)
                if disp and disp.get("id"):
                    if not any(d["id"] == disp["id"] for d in st.session_state["dispensaries"]):
                        st.session_state["dispensaries"].append(disp)
                        save_config()
                        st.success(f"Added {disp['name']}!")
                        st.rerun()
                    else:
                        st.info("Already added.")
                else:
                    st.error("Couldn't resolve that dispensary slug or URL.")
    with col_add2:
        if st.button("⚡ Presets", use_container_width=True, help="Load verified OH/KY local stores"):
            loc = st.session_state.get("home_location", "")
            lat, lng, _ = cached_geocode(loc) if loc else (None, None, None)
            existing_ids = {d["id"] for d in st.session_state.get("dispensaries", [])}
            added = 0
            for p in LOCAL_PRESETS:
                if p["id"] not in existing_ids:
                    item = dict(p)
                    if lat and lng and item.get("lat"):
                        from dutchie_discovery import haversine_miles
                        item["distance_mi"] = round(haversine_miles(lat, lng, item["lat"], item["lng"]), 1)
                    st.session_state["dispensaries"].append(item)
                    existing_ids.add(p["id"])
                    added += 1
            if added:
                save_config()
                st.success(f"Loaded {added} local OH/KY stores!")
                st.rerun()
            else:
                st.info("All presets are already added.")

    if st.session_state.get("dispensaries"):
        st.markdown(f"<span style='color:#8a94a6;font-size:12px;'>{len(st.session_state['dispensaries'])} store(s) selected</span>", unsafe_allow_html=True)
        for idx, d in enumerate(st.session_state["dispensaries"]):
            c1, c2 = st.columns([7, 1])
            dist = f" · {d['distance_mi']} mi" if d.get("distance_mi") is not None else ""
            name = html.escape(str(d.get("name", "")), quote=False)
            c1.markdown(f"<span style='font-size:13px;color:#e7ecf3;'><span class='store-dot'></span>{name}<span style='color:#8a94a6;'>{dist}</span></span>", unsafe_allow_html=True)
            if c2.button("✕", key=f"del_{idx}", help="Remove"):
                st.session_state["dispensaries"].pop(idx)
                save_config()
                st.rerun()
        if st.button("Clear all", use_container_width=True):
            st.session_state["dispensaries"] = []
            save_config()
            st.rerun()
    else:
        st.markdown("<span style='color:#8a94a6;font-size:12px;'>No stores yet — discover by location above.</span>", unsafe_allow_html=True)

    st.markdown("<hr style='border-color:rgba(255,255,255,0.08);margin:22px 0;'>", unsafe_allow_html=True)
    st.markdown("**🎛️ Filters**")
    st.slider("Price range ($)", 1.0, 300.0, step=1.0, key="price_range", on_change=save_config)
    st.slider("THC range (%)", 0.0, 100.0, step=1.0, key="thc_range", on_change=save_config)
    st.pills("Strains", ALL_STRAINS, selection_mode="multi", key="strains", on_change=save_config)
    st.pills("Categories", ALL_CATEGORIES, selection_mode="single", key="categories", on_change=save_config)

    sel_cat = st.session_state.get("categories")
    cat = sel_cat if isinstance(sel_cat, str) and sel_cat else None
    buckets = WEIGHT_OPTIONS.get(cat, []) if cat else []
    if buckets:
        labels = [lbl for lbl, _, _ in buckets]
        weight_labels = st.pills(
            "Weight / size",
            labels,
            selection_mode="multi",
            key=f"weight_{cat}",
        )
        st.session_state["weight_labels"] = list(weight_labels or [])
    else:
        st.session_state["weight_labels"] = []

    st.markdown("<hr style='border-color:rgba(255,255,255,0.08);margin:22px 0;'>", unsafe_allow_html=True)
    if st.button("↻ Clear cache & re-scan fresh", use_container_width=True):
        st.cache_data.clear()
        st.session_state.pop("last_deals", None)
        st.rerun()
# ══════════════════════════════════════════════════════════════════════════════
#  Scan logic
# ══════════════════════════════════════════════════════════════════════════════
def run_scan():
    dispensaries = st.session_state.get("dispensaries", [])
    if not dispensaries:
        st.warning("Add dispensaries first — use 🔭 discovery, ➕ manual add, or ⚡ Presets in the sidebar.")
        return
    n = len(dispensaries)
    all_rows = []
    succeeded = 0
    timed_out = 0
    errors = 0

    with st.status(f"Scanning {n} dispensaries in parallel…", expanded=True) as status_box:
        progress_bar = st.progress(0.0)

        def on_store_done(res, done_count, total_count):
            progress_bar.progress(done_count / total_count, text=f"Scanned {done_count}/{total_count} stores…")
            st_name = res.get("name", "Store")
            st_status = res.get("status", "ok")
            st_elapsed = res.get("elapsed", 0.0)
            st_prods = res.get("products", 0)
            st_rows = len(res.get("rows", []))
            if st_status == "ok":
                status_box.write(f"✔ **{st_name}**: {st_rows} options ({st_prods} products) in {st_elapsed}s")
            elif st_status == "partial":
                status_box.write(f"⚠️ **{st_name}**: partial scan ({st_rows} options) — {res.get('error')}")
            elif st_status == "timeout":
                status_box.write(f"⏱️ **{st_name}**: timed out ({res.get('error')})")
            else:
                status_box.write(f"❌ **{st_name}**: failed ({res.get('error')})")

        results = scrape_many(
            dispensaries,
            max_workers=MAX_WORKERS,
            per_store_timeout=STORE_DEADLINE,
            on_result=on_store_done,
        )

        for res in results:
            all_rows.extend(res.get("rows", []))
            st_status = res.get("status")
            if st_status == "ok":
                succeeded += 1
            elif st_status in ("partial", "timeout"):
                timed_out += 1
            else:
                errors += 1

        status_box.write(f"Filtering & scoring {len(all_rows)} options across {n} stores…")
        deals = filter_rows(
            all_rows,
            st.session_state["price_range"],
            st.session_state["thc_range"],
            st.session_state["strains"],
            st.session_state["categories"],
            st.session_state.get("weight_labels"),
        )
        compute_deal_scores(deals)
        add_savings(deals)
        st.session_state["last_deals"] = deals
        st.session_state["last_scan_time"] = time.time()
        st.session_state["last_total_rows"] = len(all_rows)
        st.session_state["last_scan_summary"] = {
            "total": n,
            "succeeded": succeeded,
            "timed_out": timed_out,
            "errors": errors,
            "options": len(all_rows),
            "deals": len(deals),
        }

        status_label = f"Scan complete! {succeeded}/{n} stores succeeded"
        if timed_out:
            status_label += f", {timed_out} timed out"
        if errors:
            status_label += f", {errors} failed"
        status_box.update(label=status_label, state="complete", expanded=False)
        progress_bar.empty()


# ── Header ──────────────────────────────────────────────────────────────────────
st.markdown("""
<div class="hero-banner">
  <h1>🌿 Best Value Deals</h1>
  <p>Compare live menus across dispensaries near you — ranked by $/gram and THC-per-dollar.</p>
</div>
""", unsafe_allow_html=True)

scan_clicked = st.button("🚀 Scan live deals", type="primary")
if scan_clicked:
    run_scan()

deals = st.session_state.get("last_deals", [])

if not deals:
    st.info("Add dispensaries in the sidebar, then hit “🚀 Scan live deals” to find the best value near you.")
    st.stop()

df = pd.DataFrame(deals)
best_ppg = df["price_per_gram"].min() if df["price_per_gram"].notna().any() else None
best_thc = df["thc"].max() if df["thc"].notna().any() else None
best_value = df["thc_mg_per_dollar"].max() if df["thc_mg_per_dollar"].notna().any() else None
stores = df["store"].nunique()
best_thc_str = f"{best_thc:.1f}%" if best_thc is not None else "—"

kpi = (
    f"<div class='kpi-row'>"
    f"<div class='kpi-card'><div class='kpi-label'>🛒 Deals found</div><div class='kpi-value'>{len(deals)}</div><div class='kpi-sub'>matching your filters</div></div>"
    f"<div class='kpi-card'><div class='kpi-label'>💵 Best $/gram</div><div class='kpi-value' style='color:#10b981;'>{fmt_money(best_ppg)}</div><div class='kpi-sub'>lowest unit price</div></div>"
    f"<div class='kpi-card'><div class='kpi-label'>🧪 Best THC</div><div class='kpi-value' style='color:#c084fc;'>{best_thc_str}</div><div class='kpi-sub'>highest potency</div></div>"
    f"<div class='kpi-card'><div class='kpi-label'>⚡ Best value</div><div class='kpi-value' style='color:#f5c542;'>{best_value if best_value is not None else '—'}</div><div class='kpi-sub'>mg THC per dollar</div></div>"
    f"<div class='kpi-card'><div class='kpi-label'>🏬 Stores</div><div class='kpi-value'>{stores}</div><div class='kpi-sub'>scanned live</div></div>"
    f"</div>"
)
st.markdown(kpi, unsafe_allow_html=True)

summary = st.session_state.get("last_scan_summary")
if summary:
    badge_color = "#10b981" if (summary["timed_out"] == 0 and summary["errors"] == 0) else "#f59e0b"
    timeout_html = f", <span style='color:#f59e0b;'>{summary['timed_out']} timed out</span>" if summary.get("timed_out") else ""
    error_html = f", <span style='color:#ef4444;'>{summary['errors']} failed</span>" if summary.get("errors") else ""
    st.markdown(
        f"<div style='font-size:12px;color:#8a94a6;margin:4px 0 16px 2px;display:flex;align-items:center;gap:8px;'>"
        f"<span style='display:inline-block;width:7px;height:7px;border-radius:50%;background:{badge_color};'></span>"
        f"<span>Scan summary: <b>{summary['succeeded']}/{summary['total']}</b> stores succeeded "
        f"({summary['options']:,} options scanned{timeout_html}{error_html})</span></div>",
        unsafe_allow_html=True,
    )
# ══════════════════════════════════════════════════════════════════════════════
#  Rendering helpers
# ══════════════════════════════════════════════════════════════════════════════
def deal_card_html(deal, rank=None):
    esc = html.escape  # attribute values (href/src) — also escape quotes
    esc_text = lambda s: html.escape(s or "", quote=False)  # text nodes — escape & < > only
    strain = deal.get("strain") or ""
    scls = strain_class(strain)
    cat = deal.get("mapped_cat") or "Other"

    badges = f"<span class='badge category'>{esc_text(cat)}</span>"
    if strain:
        badges += f" <span class='badge {scls}'>{esc_text(strain)}</span>"
    badges += f" <span class='badge score'>★ {deal.get('deal_score', 0)}</span>"
    if deal.get("savings") and deal["savings"] > 0:
        badges += f" <span class='badge save'>▼ save {fmt_money(deal['savings'])}</span>"

    # Image with a fallback layer underneath; products without an image show
    # the category emoji placeholder instead.
    emoji = CATEGORY_EMOJI.get(cat, "🌿")
    img = deal.get("image")
    fallback_html = f"<div class='deal-img-fallback'><span class='fallback-emoji'>{emoji}</span></div>"
    if img:
        img_html = (
            fallback_html
            + f"<img class='deal-img' src='{esc(img)}' loading='lazy' referrerpolicy='no-referrer'>"
        )
    else:
        img_html = fallback_html

    rank_badge = f"<span class='rank-badge'>#{rank}</span>" if rank else ""
    ppg = f"${deal['price_per_gram']:.2f}/g" if deal.get("price_per_gram") else "—/g"
    dist = f" · {deal['distance_mi']} mi" if deal.get("distance_mi") is not None else ""
    if cat == "Edibles" and deal.get("thc_mg_total"):
        thc = f"{deal['thc_mg_total']:g}mg THC"
    else:
        thc = f"{deal['thc']:.1f}% THC" if deal.get("thc") else ""
    price_total = f"${deal['price']:.2f}" if deal.get("price") is not None else "—"

    extras = []
    if deal.get("cbd"):
        extras.append(f"CBD {deal['cbd']:.1f}%")
    if deal.get("terpenes"):
        extras.append(f"{deal['terpenes']:.2f}% terps")
    if deal.get("brand"):
        extras.append(esc_text(deal["brand"]))
    extra_html = f"<div class='deal-sub'>{' · '.join(extras)}</div>" if extras else ""

    raw_option = deal.get("option") or ""
    raw_name = deal.get("name") or ""
    option = esc_text(raw_option)
    name = esc_text(raw_name)
    store = esc_text(deal.get("store") or "")
    opt_html = f"<div class='deal-sub'>{option}</div>" if raw_option and raw_option.lower() not in raw_name.lower() else ""

    slug = deal.get("slug") or ""
    product_handle = deal.get("product_slug") or deal.get("id") or ""
    product_url = deal.get("product_url") or (f"https://dutchie.com/dispensary/{slug}/product/{product_handle}" if slug else "")
    store_url = deal.get("store_url") or (f"https://dutchie.com/dispensary/{slug}" if slug else "")

    # Expanded detail rows — shown when the card is clicked anywhere.
    details = [("Product", name)]
    if deal.get("brand"):
        details.append(("Brand", esc_text(deal["brand"])))
    details.append(("Category", esc_text(cat)))
    if strain:
        details.append(("Strain", esc_text(strain)))
    if raw_option and raw_option.lower() not in raw_name.lower():
        details.append(("Option", option))
    if deal.get("weight_g"):
        details.append(("Weight", f"{deal['weight_g']} g"))
    details.append(("Price", price_total))
    details.append(("Price / gram", ppg))
    if deal.get("thc"):
        details.append(("THC", f"{deal['thc']:.1f}%"))
    if deal.get("cbd"):
        details.append(("CBD", f"{deal['cbd']:.1f}%"))
    if deal.get("terpenes"):
        details.append(("Terpenes", f"{deal['terpenes']:.2f}%"))
    if deal.get("thc_mg_total"):
        details.append(("Total THC", f"{deal['thc_mg_total']:g} mg"))
    if deal.get("thc_mg_per_dollar"):
        details.append(("mg THC / $", f"{deal['thc_mg_per_dollar']:.2f}"))
    details.append(("Store", f"{store}{dist}"))

    detail_rows = "".join(
        f"<div class='detail-row'><span class='k'>{k}</span><span class='v'>{v}</span></div>"
        for k, v in details
    )

    # The ONLY links on the card live in this footer, so clicking the card body
    # never navigates away — it simply expands the details above.
    if product_url:
        listing_btn = f"<a class='deal-btn deal-btn-primary' href='{esc(product_url)}' target='_blank' rel='noopener'>↗ View listing</a>"
    else:
        listing_btn = "<span class='deal-btn deal-btn-disabled'>↗ Listing unavailable</span>"
    if store_url:
        find_btn = f"<a class='deal-btn' href='{esc(store_url)}' target='_blank' rel='noopener'>📍 Find at {store}</a>"
    else:
        find_btn = f"<span class='deal-btn deal-btn-disabled'>📍 {store}</span>"

    card = f"""
<details class="deal-card">
  <summary class="deal-summary">
    <div class="deal-imgwrap">
      {img_html}
      {rank_badge}
    </div>
    <div class="deal-body">
      <div class="deal-store"><span class="store-dot"></span>{store}{dist}</div>
      <div class="deal-name">{name}</div>
      {opt_html}
      <div>{badges}</div>
      <div class="deal-metrics">
        <span class="deal-price">{ppg}</span>
        <span class="deal-sub">{price_total} total</span>
        <span class="deal-sub" style="margin-left:auto;">{thc}</span>
      </div>
      {extra_html}
    </div>
    <div class="deal-hint"><span class="hint-more">Tap for details ▾</span><span class="hint-less">Hide details ▴</span></div>
  </summary>
  <div class="deal-details">
    {detail_rows}
  </div>
  <div class="deal-links">
    {listing_btn}
    {find_btn}
  </div>
</details>"""
    # CommonMark ends a raw-HTML block at the first blank line; empty
    # interpolations (opt_html / rank_badge / extra_html) would otherwise leave
    # blank lines that render the rest of the card as a literal code block.
    return "\n".join(line for line in card.splitlines() if line.strip())


def render_grid(cards, cols=3):
    col_objs = st.columns(cols)
    for i, html in enumerate(cards):
        with col_objs[i % cols]:
            st.markdown(html, unsafe_allow_html=True)
# ══════════════════════════════════════════════════════════════════════════════
#  Tabs
# ══════════════════════════════════════════════════════════════════════════════
tabs = st.tabs(["🏆 Overview", "🔥 Best Deals", "📋 All Listings", "📊 Analysis", "🏬 Compare Stores"])

with tabs[0]:
    top = sort_deals(deals, "value")[:6]
    cards = [deal_card_html(d, i + 1) for i, d in enumerate(top)]
    st.markdown("<div class='section-title'>Top value picks</div>", unsafe_allow_html=True)
    render_grid(cards, 3)

    c1, c2 = st.columns(2)
    with c1:
        st.markdown("<div class='section-title'>Potency vs price</div>", unsafe_allow_html=True)
        p = df.dropna(subset=["price", "thc"])
        if not p.empty:
            st.scatter_chart(p, x="price", y="thc", color="store")
    with c2:
        st.markdown("<div class='section-title'>Avg $/gram by category</div>", unsafe_allow_html=True)
        g = df.dropna(subset=["price_per_gram"])
        if not g.empty:
            st.bar_chart(g.groupby("mapped_cat")["price_per_gram"].mean())

with tabs[1]:
    c1, c2, c3 = st.columns([2, 1, 1])
    with c1:
        search = st.text_input("Search deals", placeholder="brand, strain, product, store…")
    with c2:
        sort_key = st.selectbox(
            "Sort by",
            ["value", "price_per_gram", "thc", "price", "savings", "distance"],
            format_func=lambda s: {"value": "★ Value", "price_per_gram": "$/gram", "thc": "THC %", "price": "Total price", "savings": "Savings", "distance": "Distance"}[s],
        )
    with c3:
        cat_chip = st.selectbox("Category", ["All"] + ALL_CATEGORIES)

    sel = deals
    if search:
        q = search.lower()
        sel = [d for d in sel if q in (d.get("name") or "").lower()
               or q in (d.get("brand") or "").lower()
               or q in (d.get("strain") or "").lower()
               or q in (d.get("store") or "").lower()]
    if cat_chip != "All":
        sel = [d for d in sel if d.get("mapped_cat") == cat_chip]
    sel = sort_deals(sel, sort_key)

    st.markdown(f"<div class='section-title'>{len(sel)} deals</div>", unsafe_allow_html=True)
    if sel:
        render_grid([deal_card_html(d, i + 1) for i, d in enumerate(sel[:60])], 3)
    else:
        st.info("No deals match those filters.")
with tabs[2]:
    display = []
    for d in deals:
        slug = d.get("slug", "")
        product_handle = d.get("product_slug") or d.get("id") or ""
        purl = d.get("product_url") or (f"https://dutchie.com/dispensary/{slug}/product/{product_handle}" if slug else "")
        display.append({
            "Store": d["store"],
            "Distance (mi)": d.get("distance_mi"),
            "Product": d["name"],
            "Brand": d.get("brand", ""),
            "Strain": d.get("strain", ""),
            "Category": d.get("mapped_cat", ""),
            "Option": d.get("option", ""),
            "Price": d["price"],
            "$/g": d.get("price_per_gram"),
            "THC %": d["thc"],
            "CBD %": d.get("cbd"),
            "Terps %": d.get("terpenes"),
            "mg THC/$": d.get("thc_mg_per_dollar"),
            "Score": d.get("deal_score", 0),
            "Save": d.get("savings", 0.0),
            "Deal": purl,
        })
    ddf = pd.DataFrame(display).sort_values("Score", ascending=False)
    st.download_button("⬇ Download CSV", ddf.to_csv(index=False).encode("utf-8"), "dutchie_deals.csv", "text/csv")
    st.dataframe(
        ddf,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Deal": st.column_config.LinkColumn("Open", display_text="↗"),
            "Price": st.column_config.NumberColumn("Price", format="$%.2f"),
            "$/g": st.column_config.NumberColumn("$/g", format="$%.2f"),
            "THC %": st.column_config.NumberColumn("THC %", format="%.1f%%"),
            "CBD %": st.column_config.NumberColumn("CBD %", format="%.1f%%"),
            "Terps %": st.column_config.NumberColumn("Terps %", format="%.2f%%"),
            "mg THC/$": st.column_config.NumberColumn("mg THC/$", format="%.1f"),
            "Save": st.column_config.NumberColumn("Save", format="$%.2f"),
            "Distance (mi)": st.column_config.NumberColumn("Distance (mi)", format="%.1f"),
        },
    )

with tabs[3]:
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("<div class='section-title'>Potency vs price</div>", unsafe_allow_html=True)
        p = df.dropna(subset=["price", "thc"])
        if not p.empty:
            st.scatter_chart(p, x="price", y="thc", color="store")
        st.markdown("<div class='section-title'>Avg $/gram by store</div>", unsafe_allow_html=True)
        g = df.dropna(subset=["price_per_gram"])
        if not g.empty:
            st.bar_chart(g.groupby("store")["price_per_gram"].mean())
    with c2:
        st.markdown("<div class='section-title'>Avg $/gram by category</div>", unsafe_allow_html=True)
        g = df.dropna(subset=["price_per_gram"])
        if not g.empty:
            st.bar_chart(g.groupby("mapped_cat")["price_per_gram"].mean())
        st.markdown("<div class='section-title'>Deals by category</div>", unsafe_allow_html=True)
        st.bar_chart(df["mapped_cat"].value_counts())

with tabs[4]:
    agg = df.groupby("store").agg(
        deals=("name", "count"),
        avg_price_g=("price_per_gram", "mean"),
        best_price_g=("price_per_gram", "min"),
        avg_thc=("thc", "mean"),
        best_thc=("thc", "max"),
        avg_mg_per_dollar=("thc_mg_per_dollar", "mean"),
    ).reset_index().sort_values("avg_price_g")
    st.markdown("<div class='section-title'>Store comparison</div>", unsafe_allow_html=True)
    st.dataframe(
        agg,
        use_container_width=True,
        hide_index=True,
        column_config={
            "deals": st.column_config.NumberColumn("Deals"),
            "avg_price_g": st.column_config.NumberColumn("Avg $/g", format="$%.2f"),
            "best_price_g": st.column_config.NumberColumn("Best $/g", format="$%.2f"),
            "avg_thc": st.column_config.NumberColumn("Avg THC", format="%.1f%%"),
            "best_thc": st.column_config.NumberColumn("Best THC", format="%.1f%%"),
            "avg_mg_per_dollar": st.column_config.NumberColumn("Avg mg THC/$", format="%.1f"),
        },
    )
    st.markdown("<div class='section-title'>Avg $/gram by store</div>", unsafe_allow_html=True)
    st.bar_chart(agg.set_index("store")["avg_price_g"])








