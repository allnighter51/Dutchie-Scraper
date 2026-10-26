# Dutchie Deal Finder

A feature-rich Streamlit app that discovers dispensaries near you, scans their
live menus via Dutchie's internal GraphQL API, and ranks the absolute best value
deals by **$ per gram** and **THC-per-dollar**.

## Features

- **📍 Location discovery** — enter a ZIP code or address and the app finds every
  Dutchie dispensary within a radius (via Dutchie's `ConsumerDispensaries`
  GraphQL query + OpenStreetMap geocoding, no API key needed).
- **🔥 Value ranking** — a composite "deal score" based on mg THC per dollar and
  $/gram, plus savings vs. the category median.
- **🎨 Polished UI** — dark theme, KPI cards, product-image deal cards, and five
  tabbed views (Overview, Best Deals, All Listings, Analysis, Compare Stores).
- **🎛️ Filters & search** — price range, THC range, strain, category, free-text
  search, and multiple sort orders.
- **📊 Charts & export** — potency vs. price, $/g by store & category, store
  comparison table, and CSV download.
- **⚡ Caching** — menu scans and geocoding are cached so re-runs are instant.

## Quick Start

```powershell
pip install curl_cffi requests streamlit pandas
streamlit run app.py
```

## How it works

1. **Geocode** your ZIP/address with Nominatim (OpenStreetMap).
2. **Discover** nearby dispensaries with Dutchie's `ConsumerDispensaries`
   GraphQL query (`nearLat` / `nearLng` / `distance` in meters, `sortBy: distance`).
3. **Scan** each store's menu with the `FilteredProducts` query (Cloudflare
   bypass via `curl_cffi` `impersonate="chrome120"`).
4. **Extract** THC from lab data (`THCContent` % → `cannabinoidsV2` → scalar),
   plus CBD, terpenes, brand, and the product image.
5. **Rank** deals by mg THC per dollar and $/gram, then surface the best.

## Notes

- THC potency is only available for stores that upload lab COAs to Dutchie.
  Products priced by mg (many edibles/topicals) are excluded from the value
  ranking because they lack a % THC figure.
- The scanner respects Dutchie's page size and adds a delay between pages.
- `dutchie_scraper.py` can still be run standalone (`python dutchie_scraper.py`)
  to print a CLI report and optionally post to a Discord webhook.

## Project layout

| File | Purpose |
|---|---|
| `app.py` | Streamlit UI |
| `dutchie_scraper.py` | Menu scraping (`FilteredProducts` GraphQL) |
| `dutchie_discovery.py` | Geocoding + nearby-dispensary discovery |
| `dutchie_config.json` | Saved stores, filters, and location |

## Adding a dispensary manually

Enter a Dutchie slug (e.g. `shangri-la-amelia`) or a full store URL in the
sidebar, or use the 🔭 discovery button to auto-populate stores near you.
