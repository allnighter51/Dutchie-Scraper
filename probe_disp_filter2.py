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
query ConsumerDispensaries($dispensaryFilter: dispensariesFilterInput!) {
  filteredDispensaries(filter: $dispensaryFilter) {
    id
    name
    cName
  }
}
"""
test_filters = [
    {"city": "Batavia", "cName": "batavia"},
    {"cName": "shangri-la-amelia"},
    {"cNameEQ": "shangri-la-amelia"},
    {"near": "Batavia, OH"}, # Maybe near is a string?
    {"search": "Batavia"},
    {"search": "Ohio"},
]

for tf in test_filters:
    print(f"\nTesting filter: {tf}")
    r = session.post("https://dutchie.com/graphql", headers=gql_headers, json={"query": query, "variables": {"dispensaryFilter": tf}})
    if "errors" in r.json():
        print("Error:", r.json()["errors"][0]["message"])
    else:
        dispensaries = r.json().get('data', {}).get('filteredDispensaries', [])
        print(f"Success! Found {len(dispensaries)} dispensaries.")
        if dispensaries:
            print(dispensaries[0])
