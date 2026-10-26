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
query {
  __type(name: "dispensariesFilterInput") {
    name
    inputFields {
      name
      type { name kind ofType { name kind } }
    }
  }
}
"""

r = session.post(
    "https://dutchie.com/graphql", 
    headers=gql_headers, 
    json={"query": query}
)
print(json.dumps(r.json(), indent=2))
