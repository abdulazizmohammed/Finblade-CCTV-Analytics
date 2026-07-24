import urllib.request, sys
try:
    urllib.request.urlopen("https://pypi.org", timeout=5)
    print("NETWORK: UP")
except Exception as e:
    print(f"NETWORK: DOWN ({type(e).__name__})")
