#!/usr/bin/env bash
# Does write-on-change actually reduce writes on the running service?
#
# Part A step 4. The unit tests prove the gate's logic; this proves the gate is
# wired into the process that is actually serving, with real cameras posting.
# Read-only apart from the zone-state posts the cameras were making anyway.
set -u

API="${FINBLADE_API:-http://127.0.0.1:8000}"
KEY="${FINBLADE_API_KEY:-$(cat .local_key 2>/dev/null)}"
DB="${FINBLADE_DB:-data/finblade.db}"
WINDOW="${1:-60}"

hdr=(-H "X-API-Key: ${KEY}")

echo "== service reachable?"
if ! curl -fsS --max-time 5 "${API}/healthz" >/dev/null 2>&1; then
  echo "  no API at ${API} — start it first (scripts/start_demo.sh)"
  exit 1
fi
echo "  ok"

echo
echo "== gate configuration as the running process sees it"
curl -fsS --max-time 5 "${hdr[@]}" "${API}/api/v1/health" \
  | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["checks"].get("state_writes"), indent=2))'

echo
echo "== counting zone_state_ts rows over ${WINDOW}s of live traffic"
before=$(sqlite3 "file:${DB}?mode=ro" "SELECT COUNT(*) FROM zone_state_ts")
posts_before=$(curl -fsS --max-time 5 "${hdr[@]}" "${API}/api/v1/health" \
  | python3 -c 'import json,sys; s=json.load(sys.stdin)["checks"]["state_writes"]; print(s["written"]+s["suppressed"])')

sleep "${WINDOW}"

after=$(sqlite3 "file:${DB}?mode=ro" "SELECT COUNT(*) FROM zone_state_ts")
posts_after=$(curl -fsS --max-time 5 "${hdr[@]}" "${API}/api/v1/health" \
  | python3 -c 'import json,sys; s=json.load(sys.stdin)["checks"]["state_writes"]; print(s["written"]+s["suppressed"])')

posts=$((posts_after - posts_before))
rows=$((after - before))
echo "  posts received : ${posts}"
echo "  rows appended  : ${rows}"
if [ "${posts}" -gt 0 ]; then
  python3 -c "print('  suppressed     : %.1f%%' % (100.0*(${posts}-${rows})/${posts}))"
else
  echo "  no posts arrived — are any cameras running?"
fi

echo
echo "== the live reading must still be current for every zone"
# The failure this guards against: history is suppressed AND the live row goes
# stale, so quiet zones drop out of /zones/state after 30s and the dashboard
# empties itself precisely when nothing is happening.
curl -fsS --max-time 5 "${hdr[@]}" "${API}/api/v1/zones/state" \
  | python3 -c '
import json, sys, time
rows = json.load(sys.stdin)
rows = rows.get("zones", rows) if isinstance(rows, dict) else rows
now = time.time()
if not rows:
    print("  no zones reporting — cannot tell freshness from absence")
    raise SystemExit(0)
for r in sorted(rows, key=lambda r: (r.get("camera_id") or "", r.get("zone_id") or "")):
    age = now - float(r.get("ts") or 0)
    flag = "STALE" if age > 30 else "ok"
    print(f"  {r.get(\"camera_id\"):<10} {r.get(\"zone_id\"):<10} occ={r.get(\"occupancy\")}"
          f" age={age:5.1f}s {flag}")
print(f"  {len(rows)} zone(s) live")
'
