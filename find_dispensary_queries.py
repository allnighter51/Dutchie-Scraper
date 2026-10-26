import re
from curl_cffi import requests as cf_requests

session = cf_requests.Session(impersonate="chrome120")

# Fetch homepage
r = session.get("https://dutchie.com")
html = r.text

# Find all script tags with src
js_files = re.findall(r'<script[^>]+src="([^"]+\.js)"', html)
print("JS files found:", js_files)

# Download and search them for GraphQL queries
for js_url in js_files:
    if js_url.startswith('/'):
        js_url = "https://dutchie.com" + js_url
    print(f"Searching {js_url}...")
    try:
        r = session.get(js_url)
        content = r.text
        
        # Look for GraphQL queries
        queries = re.findall(r'query\s+(\w+)\s*\(', content)
        if queries:
            print(f"  Found queries: {set(queries)}")
            
        # Check for dispensary queries
        if 'Dispensaries' in content or 'dispensaries' in content:
            print("  Contains 'dispensaries' keywords.")
            matches = re.findall(r'query\s+\w+.*?\{.*?\}', content, re.DOTALL)
            for m in matches:
                if 'dispensar' in m.lower():
                    print(f"  --- Query Match ---\n{m[:500]}...\n")
    except Exception as e:
        print(f"  Error: {e}")
