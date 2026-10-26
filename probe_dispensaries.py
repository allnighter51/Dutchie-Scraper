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

# The query for dispensaries near a location is likely called Dispensaries or DispensariesSearch
# Let's try a broad introspection or just guessing the query based on common Dutchie patterns.
query = """
query {
  dispensaries(filter: { near: { lat: 39.077, lng: -84.177 }, distance: 50 }) {
    id
    name
    cName
    location { address1 city state zip }
  }
}
"""

r = session.post("https://dutchie.com/graphql", headers=gql_headers, json={"query": query})
print(json.dumps(r.json(), indent=2))
