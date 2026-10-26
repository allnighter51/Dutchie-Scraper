import sys
import json
from curl_cffi import requests as cf_requests

session = cf_requests.Session(impersonate="chrome120")

# Test 1: REST API
print("Testing REST API...")
r = session.get("https://dutchie.com/api/v2/dispensaries?lat=39.077&lng=-84.177")
if r.status_code == 200:
    print(r.text[:500])
else:
    print(r.status_code, r.text[:100])

# Test 2: GraphQL with different variable structures
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
    {"cName": "ohio"}, # Test state
    {"city": "Batavia"}, # Test city
    {"state": "OH"},
    {"location": {"state": "OH"}},
    {"retailer": "uplift"}
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
