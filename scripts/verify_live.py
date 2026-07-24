import json, time, urllib.request

def get(path):
    with urllib.request.urlopen("http://127.0.0.1:8000" + path, timeout=2) as r:
        return json.loads(r.read().decode())

zones = []
for i in range(40):  # up to ~80s for the model to load + first 5s aggregate
    try:
        z = get("/api/v1/zones/state").get("zones", [])
        if z:
            zones = z
            break
    except Exception:
        pass
    time.sleep(2)

print("=== live zone states ===")
for zs in zones:
    print(f"  {zs.get('zone_id'):16} occ={zs.get('occupancy'):>2} "
          f"dens={float(zs.get('density',0)):.2f}/m2 cap={float(zs.get('capacity_pct',0)):.0f}% "
          f"status={zs.get('status')} in/out={zs.get('inflow_per_min'):.1f}/{zs.get('outflow_per_min'):.1f}")
if not zones:
    print("  (none yet)")

try:
    alerts = get("/api/v1/alerts?unacked_only=true").get("alerts", [])
except Exception:
    alerts = []
print(f"=== live unacked alerts: {len(alerts)} ===")
by_rule = {}
for a in alerts:
    by_rule[a.get("rule_id")] = by_rule.get(a.get("rule_id"), 0) + 1
for rid, n in sorted(by_rule.items()):
    print(f"  {rid}: {n}")
for a in alerts[:4]:
    print("  -", a.get("rule_id"), a.get("severity"), "|", a.get("message"),
          "| id=", a.get("alert_id"))
