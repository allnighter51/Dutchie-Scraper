import sys
import json
from curl_cffi import requests as cf_requests

session = cf_requests.Session(impersonate="chrome120")
gql_headers = {
    "accept": "application/json",
    "apollographql-client-name": "desktop-web",
    "content-type": "application/json",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
}

query = """
query FilteredProducts($f: productsFilterInput!) {
  filteredProducts(filter: $f, page: 1, perPage: 10) {
    products {
      Name
      strainType
      type
      Options
      Prices
      THC
      THCContent { unit range }
      cannabinoidsV2 { value unit cannabinoid { name } }
      totalTerpenes { unit range }
    }
  }
}
"""

for disp_id, name in [("69265b5d6fb4bb7d84f4a82a", "Shangri-La"), ("633deac5361a0700d6564010", "Uplift")]:
    print(f"\n--- {name} ---")
    f = {"dispensaryId": disp_id, "Status": "Active"}
    r = session.post("https://dutchie.com/graphql", headers=gql_headers, json={"query": query, "variables": {"f": f}})
    prods = r.json().get('data', {}).get('filteredProducts', {}).get('products', [])
    for p in prods:
        if p.get('type') == 'Flower':
            print(json.dumps(p, indent=2))
            break
    else:
        if prods:
            print("No flower found, printing first:")
            print(json.dumps(prods[0], indent=2))
