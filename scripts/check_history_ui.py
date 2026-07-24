import json, time, urllib.request
def get(p):
    with urllib.request.urlopen("http://127.0.0.1:8000"+p, timeout=3) as r:
        return r.status, r.read()
for p in ["/web/history.html", "/web/dashboard.html", "/web/logo.svg"]:
    s,b=get(p); print(s, p, len(b), "bytes")
now=time.time()
a=json.loads(get(f"/api/v1/history/alerts?from=0&to={now+1e6}")[1])["alerts"]
print("history alerts:", len(a), "with frame:", sum(1 for x in a if x.get('frame')))
if a:
    x=a[0]; print("sample:", x['rule_id'], time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(x['ts'])), x.get('frame'))
