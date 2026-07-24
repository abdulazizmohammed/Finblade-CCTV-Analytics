import json, time, urllib.request

def get(p):
    with urllib.request.urlopen("http://127.0.0.1:8000"+p, timeout=2) as r:
        return json.loads(r.read().decode())

zones=[]
for _ in range(40):
    try:
        zones=get("/api/v1/zones/state").get("zones",[])
        if zones: break
    except Exception: pass
    time.sleep(2)

print("=== zone-state payload (enriched) ===")
for z in zones:
    print(f"  {z.get('zone_id')}: name={z.get('zone_name')!r} restricted={z.get('restricted')} "
          f"occ={z.get('occupancy')} dens={z.get('density')} status={z.get('status')}")
have_keys = all(('zone_name' in z and 'restricted' in z) for z in zones) if zones else False
print("enriched keys present:", have_keys)

try:
    a=get("/api/v1/alerts?unacked_only=true").get("alerts",[])
    print("unacked alerts:", len(a), "rules:", sorted({x.get('rule_id') for x in a}))
except Exception as e:
    print("alerts err", e)

# dashboard served?
for p in ["/web/dashboard.html","/web/finblade-theme.css"]:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000"+p, timeout=3) as r:
            print(f"{r.status} {p} ({len(r.read())} bytes)")
    except Exception as e:
        print("ERR", p, e)
