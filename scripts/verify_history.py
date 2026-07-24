import json, os, time, urllib.request

def get(p):
    with urllib.request.urlopen("http://127.0.0.1:8000"+p, timeout=3) as r:
        return json.loads(r.read().decode())

# wait for zone data to appear (runner warming up)
for _ in range(40):
    try:
        if get("/api/v1/zones/state").get("zones"): break
    except Exception: pass
    time.sleep(2)

now = time.time()
ev = get(f"/api/v1/history/events?from=0&to={now+1e6}&limit=1000").get("events", [])
al = get(f"/api/v1/history/alerts?from=0&to={now+1e6}&limit=1000").get("alerts", [])
cams = get("/api/v1/cameras").get("cameras", [])

print("events:", len(ev), " types:", sorted({e['event_type'] for e in ev}))
print("alerts:", len(al), " rules:", sorted({a['rule_id'] for a in al}))
print("cameras:", [(c['camera_id'], c.get('online')) for c in cams])

# events carry wall-clock ts + some have frame bookmarks
withframe = [e for e in ev if e.get('frame')]
print("events with frame:", len(withframe))
if ev:
    e = ev[0]
    print("sample event ts:", e['ts'], "->", time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(e['ts'])),
          "type:", e['event_type'], "frame:", e.get('frame'))
if withframe:
    fr = withframe[0]['frame']
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000"+fr, timeout=3) as r:
            print("bookmark", fr, "->", r.status, len(r.read()), "bytes")
    except Exception as ex:
        print("bookmark fetch ERR", fr, ex)
alf=[a for a in al if a.get('frame')]
print("alerts with frame:", len(alf))
