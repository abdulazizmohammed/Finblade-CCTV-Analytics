#!/usr/bin/env bash
# Read-only dump of everything the facility count depends on, for review.
#
#   bash scripts/facility_diagnose.sh            # last 2 h of door traffic
#   bash scripts/facility_diagnose.sh 6          # last 6 h
#
# Prints: door zones with their polygons and types, every camera, the
# ReID/topology settings that decide whether a person keeps one global ref
# across a visit, the roster stats, how old the "inside" entries are, and the
# raw crossing events at each door (what the roster actually saw).
#
# It ends by REBUILDING the roster over the window. That is a write — it
# replaces the live roster with the one the stored events imply under today's
# zones and rules — and it is the only honest way to read the counters: the
# lifetime ones span every geometry the door has ever had, so they say nothing
# about whether it counts correctly now. Everything before that point is
# read-only.
set -u
cd "$(dirname "$0")/.."
if [ -f .env ]; then set -a; . ./.env; set +a; fi
HOURS="${1:-2}"
API="${FINBLADE_SELF_URL:-http://127.0.0.1:8000}"
K="${FINBLADE_API_KEY:-}"
PY=.venv/bin/python
get() { curl -s -m 20 ${K:+-H "Authorization: Bearer $K"} "$API$1"; }

echo "=== git: $(git log --oneline -1 2>/dev/null)   host: $(hostname)   $(date -u +%FT%TZ)"
echo
echo "=== ReID / presence settings (what keeps one global ref for one visit)"
grep -E '^(FINBLADE_REID|FINBLADE_TOPOLOGY|FINBLADE_PRESENCE|FINBLADE_SITE_ID|FINBLADE_AUTOSTART)' .env 2>/dev/null | sed 's/=.*KEY.*/=<redacted>/' || true
echo "topology file: ${FINBLADE_TOPOLOGY:-config/topology.yaml}"
[ -f "${FINBLADE_TOPOLOGY:-config/topology.yaml}" ] && sed -n '1,80p' "${FINBLADE_TOPOLOGY:-config/topology.yaml}" | grep -vE '^\s*#|^\s*$'
echo
echo "=== cameras"
get /api/v1/cameras | $PY -c '
import sys, json
for c in json.load(sys.stdin).get("cameras", []):
    print("  %-10s site=%-8s %-8s %s fps=%s" % (c.get("camera_id"), c.get("site_id"), c.get("effective_state"), c.get("resolution"), c.get("input_fps")))'
echo
echo "=== zones (type, enabled, polygon)"
get /api/v1/zones | $PY -c '
import sys, json
for z in json.load(sys.stdin).get("zones", []):
    poly = z.get("polygon") or z.get("normalized_polygon") or []
    unit = "px" if z.get("polygon") else "norm"
    print("  %-10s %-14s type=%-10s enabled=%-5s area=%-6s pts=%d %s %s" % (
        z.get("camera_id"), z.get("zone_id"), z.get("zone_type"), z.get("enabled", True),
        z.get("area_sqm"), len(poly), unit, json.dumps(poly)[:170]))'
echo
echo "=== facility roster"
get /api/v1/facility/occupancy | $PY -c '
import sys, json
d = json.load(sys.stdin)
print("  occupancy=%s observed=%s baseline=%s stale=%s pending=%s" % (d.get("occupancy"), d.get("observed"), d.get("baseline"), d.get("stale"), d.get("pending_crossings")))
print("  stats:", json.dumps(d.get("stats")))
print("  policy:", json.dumps(d.get("policy")))
for door in d.get("doors", []):
    print("  door %s entries=%s exits=%s net=%s" % (door.get("door_zone_id"), door.get("entries"), door.get("exits"), door.get("net")))'
echo
echo "=== age of the people currently 'inside' (hours since last seen)"
get /api/v1/facility/members | $PY -c '
import sys, json, time
d = json.load(sys.stdin)
ms = d.get("members") or d.get("people") or []
now = time.time()
ages = sorted((now - float(m.get("last_seen") or m.get("admitted_at") or now)) / 3600 for m in ms)
if ages:
    print("  n=%d  min=%.1fh  median=%.1fh  max=%.1fh" % (len(ages), ages[0], ages[len(ages)//2], ages[-1]))
    print("  <1h: %d   1-4h: %d   4-12h: %d   >12h: %d" % (sum(a<1 for a in ages), sum(1<=a<4 for a in ages), sum(4<=a<12 for a in ages), sum(a>=12 for a in ages)))
else:
    print("  none  (keys seen: %s)" % list(d)[:6])'
echo
echo "=== identity"
get /api/v1/identity/stats | head -c 600; echo
echo
echo "=== door crossings, last ${HOURS} h: what the roster was fed (newest first, up to 60)"
NOW=$($PY -c 'import time;print(time.time())')
FROM=$($PY -c "print($NOW - $HOURS*3600)")
get "/api/v1/history/events?from=$FROM&to=$NOW&event_type=ZONE_TRANSITION&limit=400" | $PY -c '
import sys, json, time, collections
d = json.load(sys.stdin)
evs = d.get("events", [])
doors = {"ZONE-01"}
rows = [e for e in evs if e.get("zone_from") in doors or e.get("zone_to") in doors]
print("  transitions total=%d  touching a door=%d" % (len(evs), len(rows)))
pairs = collections.Counter((e.get("zone_from"), e.get("zone_to")) for e in rows)
for (f, t), n in pairs.most_common(12):
    print("  %4d  %s -> %s" % (n, f, t))
print("  --- sample (time  cam  from -> to  person_ref  global_ref)")
for e in rows[:60]:
    ts = time.strftime("%H:%M:%S", time.localtime(float(e.get("timestamp") or 0)))
    print("  %s %-7s %-10s -> %-10s %s %s" % (ts, e.get("camera_id"), e.get("zone_from"), e.get("zone_to"),
          (e.get("person_ref") or "")[:12], (e.get("global_ref") or "-")[:14]))'
echo
echo "=== entries/exits at the door, last ${HOURS} h (ZONE_ENTRY / ZONE_EXIT on the door zone, non-derived)"
for T in ZONE_ENTRY ZONE_EXIT; do
  get "/api/v1/history/events?from=$FROM&to=$NOW&event_type=$T&zone_id=ZONE-01&limit=400" | $PY -c "
import sys, json
evs = json.load(sys.stdin).get('events', [])
raw = [e for e in evs if not e.get('derived')]
refs = {e.get('global_ref') or e.get('person_ref') for e in raw}
print('  $T on ZONE-01: %d events (%d non-derived), %d distinct refs' % (len(evs), len(raw), len(refs)))"
done
echo
echo "=== THE CLEAN READING: replay the window under the CURRENT zones and rules"
# The lifetime counters above span every geometry this door has ever had, so
# they cannot answer "is it counting correctly NOW". A rebuild re-derives the
# roster from the stored events as they would be judged today and reports what
# each crossing resolved to — which is the number to read.
NOW2=$($PY -c 'import time;print(time.time())')
SINCE=$($PY -c "print($NOW2 - $HOURS*3600)")
curl -s -m 60 ${K:+-H "Authorization: Bearer $K"} -X POST "$API/api/v1/facility/rebuild" \
  -H 'Content-Type: application/json' -d "{\"since\": $SINCE}" | $PY -c '
import sys, json
raw = sys.stdin.read().strip()
if not raw:
    print("   no response — is the API running? (systemctl status finblade-api)"); raise SystemExit
try:
    d = json.loads(raw)
except ValueError:
    print("   not JSON:", raw[:200])
    print("   a 404 here means the API predates POST /api/v1/facility/rebuild — git pull and restart")
    raise SystemExit
if d.get("error") or d.get("detail"): print("  ", d); raise SystemExit
print("  events replayed:", d.get("events_replayed"), " actions:", json.dumps(d.get("actions")))
print("  occupancy %s -> %s" % (d.get("occupancy_before"), d.get("occupancy")))
s = d.get("stats") or {}
keep = ("admitted","discharged","discharged_matched","discharged_unmatched",
        "discharge_on_empty","ambiguous_crossings","turned_back","expired","rekeyed")
print("  stats (THIS window only):", json.dumps({k: s.get(k) for k in keep}))
print()
print("  READ IT LIKE THIS:")
print("   admitted ~ the people who really came in      -> entries are resolving")
print("   discharged > admitted                         -> exits are being invented")
print("   ambiguous_crossings high                      -> a gap between the door strip and the lobby zone")
print("   turned_back high                              -> the door strip covers floor people walk along")
print("   discharge_on_empty > 0                        -> exits arriving with nobody on the roster")'
echo
echo "=== snapshot of the door camera -> /tmp/door_cam.jpg (paste it into the review)"
get /api/v1/cameras/CAM-01/snapshot > /tmp/door_cam.jpg && ls -la /tmp/door_cam.jpg
