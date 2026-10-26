import sys
import re
from curl_cffi import requests as cf_requests

session = cf_requests.Session(impersonate="chrome120")
r = session.get("https://dutchie.com/dispensaries/ohio")

# Find all links to dispensaries
# e.g., <a href="/dispensary/shangri-la-amelia">
links = re.findall(r'href="/dispensary/([^/"]+)"', r.text)
print("Found dispensaries:", set(links))

# Find city links
# e.g., <a href="/dispensaries/ohio/batavia">
cities = re.findall(r'href="/dispensaries/ohio/([^/"]+)"', r.text)
print("Found cities:", set(cities))
