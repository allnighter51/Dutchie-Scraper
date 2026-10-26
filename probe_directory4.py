import sys
import re
from curl_cffi import requests as cf_requests

session = cf_requests.Session(impersonate="chrome120")
r = session.get("https://dutchie.com/dispensaries/ohio")

import json
match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', r.text)
if match:
    data = json.loads(match.group(1))
    
    # Traverse JSON to find dispensaries
    def find_dispensaries(d):
        if isinstance(d, dict):
            if 'cName' in d and 'name' in d:
                print(d['name'], d['cName'])
            for k, v in d.items():
                find_dispensaries(v)
        elif isinstance(d, list):
            for i in d:
                find_dispensaries(i)

    find_dispensaries(data)
