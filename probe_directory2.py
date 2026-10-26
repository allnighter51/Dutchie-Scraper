import sys
import json
import re
from curl_cffi import requests as cf_requests

session = cf_requests.Session(impersonate="chrome120")
r = session.get("https://dutchie.com/dispensaries/ohio")

match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', r.text)
if match:
    data = json.loads(match.group(1))
    print(json.dumps(data, indent=2)[:2000])
