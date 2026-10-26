import re
from curl_cffi import requests as cf_requests

session = cf_requests.Session(impersonate="chrome120")

url = "https://assets2.dutchie.com/_next/static/chunks/pages/_app-6139ab39fd467373.js"
r = session.get(url)

# Extract the ConsumerDispensaries query completely
matches = re.findall(r'query\s+ConsumerDispensaries[\s\S]*?\{[\s\S]*?\}', r.text)
for m in matches:
    print(m[:1000])

# Let's also look for ActiveDispensaries
print("\n--- ActiveDispensaries ---")
matches = re.findall(r'query\s+ActiveDispensaries[\s\S]*?\{[\s\S]*?\}', r.text)
for m in matches:
    print(m[:1000])
