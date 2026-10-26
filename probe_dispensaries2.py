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
    location { address1 city state zip }
  }
}
"""

# Batavia OH coordinates: 39.077, -84.177
filter_input = {
    "near": [39.077, -84.177],
    "distance": 50 # Or try other fields if this fails
}

r = session.post(
    "https://dutchie.com/graphql", 
    headers=gql_headers, 
    json={"query": query, "variables": {"dispensaryFilter": filter_input}}
)
print(json.dumps(r.json(), indent=2))
