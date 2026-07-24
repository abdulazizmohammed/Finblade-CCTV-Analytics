import urllib.request
for path in ["/web/dashboard.html", "/web/finblade-theme.css", "/api/v1/reports/occupancy"]:
    try:
        r = urllib.request.urlopen("http://127.0.0.1:8000" + path, timeout=3)
        body = r.read()
        print(f"{r.status}  {path}  ({len(body)} bytes)")
    except Exception as e:
        print(f"ERR  {path}  {e}")
