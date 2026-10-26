import sys
import re
import json
from curl_cffi import requests as cf_requests

session = cf_requests.Session(impersonate="chrome120")

url = "https://dutchie.com/dispensary/shangri-la-amelia"
r = session.get(url)

match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', r.text)
if match:
    data = json.loads(match.group(1))
    print(json.dumps(data['props']['pageProps'], indent=2)[:500])
else:
    print("Not found")
