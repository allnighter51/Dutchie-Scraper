import sys
import re
from curl_cffi import requests as cf_requests

session = cf_requests.Session(impersonate="chrome120")

url = "https://dutchie.com/dispensary/shangri-la-amelia"
r = session.get(url)

# Just regex search for object IDs like "69265b5d6fb4bb7d84f4a82a"
# Or search for "dispensaryId":"..."
ids = re.findall(r'"dispensaryId":"([a-f0-9]{24})"', r.text)
if ids:
    print("Found dispensaryIds:", set(ids))
else:
    # Maybe it's just id?
    ids2 = re.findall(r'"id":"([a-f0-9]{24})"', r.text)
    print("Found ids:", set(ids2))
