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
query ActiveDispensaries($params: activeDispensariesInput) {
  activeDispensaries(params: $params) {
    dispensaries {
      id
      name
      cName
      location {
        city
        state
        distance
      }
    }
  }
}
"""

test_filters = [
    {"near": [39.077, -84.177], "distance": 50},
    {"lat": 39.077, "lng": -84.177, "radius": 50},
    {"location": {"lat": 39.077, "lng": -84.177}},
    {"center": [39.077, -84.177]},
    {} # Empty, maybe it returns all?
]

for tf in test_filters:
    print(f"\nTesting filter: {tf}")
    r = session.post("https://dutchie.com/graphql", headers=gql_headers, json={"query": query, "variables": {"params": tf}})
    if "errors" in r.json():
        print("Error:", r.json()["errors"][0]["message"])
    else:
        dispensaries = r.json().get('data', {}).get('activeDispensaries', {}).get('dispensaries', [])
        print(f"Success! Found {len(dispensaries)} dispensaries.")
        if dispensaries:
            print(dispensaries[0])
