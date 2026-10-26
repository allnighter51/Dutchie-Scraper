import sys
from curl_cffi import requests as cf_requests

session = cf_requests.Session(impersonate="chrome120")

# Test locations
urls = [
    "https://dutchie.com/dispensaries/oh/batavia",
    "https://dutchie.com/dispensaries/ohio",
    "https://dutchie.com/dispensaries",
    "https://dutchie.com/api/v1/dispensaries",
    "https://dutchie.com/graphql",
]

for url in urls[:2]:
    print(f"\nTesting {url}")
    r = session.get(url)
    print(r.status_code)
    # Check for __NEXT_DATA__
    import re
    match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', r.text)
    if match:
        data = match.group(1)
        print("Found NEXT_DATA, length:", len(data))
        if "shangri-la" in data:
            print("Found shangri-la in data!")
