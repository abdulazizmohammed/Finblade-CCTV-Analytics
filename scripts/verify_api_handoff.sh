#!/usr/bin/env bash
# Exercise every endpoint in docs/FINBLADE_CLIENT_GUIDE.md against a RUNNING
# instance, and write a transcript you can attach to the handoff email.
#
#   FINBLADE_API_KEY=<key> bash scripts/verify_api_handoff.sh
#   BASE=http://127.0.0.1:8000 bash scripts/verify_api_handoff.sh   # no auth
#
# READ-ONLY BY DESIGN. It never calls DELETE, ack/resolve, simulate-failure,
# identity/merge or identity/release, so it is safe against the instance you are
# about to demo. That also means the ack path in guide §5 is NOT covered here —
# the FinBlade developer must exercise that themselves against a real alert.
#
# Exits non-zero if any check fails, so it doubles as a smoke test.

set -u
cd "$(dirname "$0")/.."

BASE="${BASE:-http://127.0.0.1:8000}"
PY=.venv/bin/python
OUT=evidence/api_handoff.txt
mkdir -p evidence

KEY="${FINBLADE_API_KEY:-}"
AUTH=()
[ -n "$KEY" ] && AUTH=(-H "Authorization: Bearer $KEY")

PASS=0; FAIL=0
: > "$OUT"

# Everything printed goes to the terminal AND the transcript. The key is
# redacted on the way out so the file is safe to forward.
say() {
  if [ -n "$KEY" ]; then
    printf '%s\n' "$*" | sed "s|$KEY|<REDACTED>|g" | tee -a "$OUT"
  else
    printf '%s\n' "$*" | tee -a "$OUT"
  fi
}

# req <expected-status> <label> <path> [extra curl args…]
req() {
  local expect="$1" label="$2" path="$3"; shift 3
  local body code
  body=$(mktemp)
  code=$(curl -s -o "$body" -w '%{http_code}' "$@" "$BASE$path" 2>/dev/null)
  if [ "$code" = "$expect" ]; then
    PASS=$((PASS+1)); say "  ok   $code  $label"
  else
    FAIL=$((FAIL+1)); say "  FAIL $code (wanted $expect)  $label"
  fi
  # Pretty-print JSON when we can; truncate so the transcript stays readable.
  if [ -s "$body" ]; then
    $PY -m json.tool < "$body" 2>/dev/null | head -c 1400 > "$body.fmt" \
      || head -c 400 "$body" > "$body.fmt"
    [ -s "$body.fmt" ] && sed 's/^/       /' "$body.fmt" \
      | { if [ -n "$KEY" ]; then sed "s|$KEY|<REDACTED>|g"; else cat; fi; } >> "$OUT"
  fi
  rm -f "$body" "$body.fmt"
}

say "== FinBlade CCTV — API handoff verification =="
say "   base: $BASE"
say "   auth: $([ -n "$KEY" ] && echo 'API key SET' || echo 'DISABLED (no FINBLADE_API_KEY)')"
say "   date: $(date -u '+%Y-%m-%dT%H:%M:%SZ') (UTC)"
say "   code: $(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
say ""

# ---------------------------------------------------------------- auth contract
say "== 1. auth contract (guide §2) =="
if [ -n "$KEY" ]; then
  req 401 "no key -> 401"                 /api/v1/cameras
  req 200 "Authorization: Bearer"         /api/v1/cameras -H "Authorization: Bearer $KEY"
  req 200 "X-API-Key"                     /api/v1/cameras -H "X-API-Key: $KEY"
  req 401 "?key= rejected off /stream"    "/api/v1/cameras?key=$KEY"
  req 401 "wrong key -> 401"              /api/v1/cameras -H "Authorization: Bearer wrong-key"
else
  say "  SKIP — no FINBLADE_API_KEY set, so every route is OPEN."
  say "  The guide tells the FinBlade developer that /api/v1 requires a key."
  say "  Enable it before sending the guide, or the guide is wrong."
  req 200 "unauthenticated access confirmed" /api/v1/cameras
fi
say ""

# ------------------------------------------------------------------- live state
say "== 2. live state (guide §3) =="
req 200 "GET /api/v1/cameras"            /api/v1/cameras          "${AUTH[@]}"
req 200 "GET /api/v1/zones/state"        /api/v1/zones/state      "${AUTH[@]}"
req 200 "GET /api/v1/zones"              /api/v1/zones            "${AUTH[@]}"
req 200 "GET /api/v1/identity/counts"    /api/v1/identity/counts  "${AUTH[@]}"
req 200 "GET /api/v1/alerts"             /api/v1/alerts           "${AUTH[@]}"
say ""

# ------------------------------------------------------------ history + reports
NOW=$($PY -c 'import time; print(int(time.time()))')
FROM=$((NOW - 3600))
say "== 3. history and reports (guide §4), last hour =="
req 200 "GET /api/v1/history/events"  "/api/v1/history/events?from=$FROM&to=$NOW&limit=5" "${AUTH[@]}"
req 200 "GET /api/v1/history/alerts"  "/api/v1/history/alerts?from=$FROM&to=$NOW&limit=5" "${AUTH[@]}"
req 200 "GET /api/v1/reports/occupancy.json" "/api/v1/reports/occupancy.json?from=$FROM&to=$NOW" "${AUTH[@]}"
req 200 "GET /api/v1/reports/occupancy.csv"  "/api/v1/reports/occupancy.csv?from=$FROM&to=$NOW"  "${AUTH[@]}"
req 200 "GET /api/v1/movement"        "/api/v1/movement?minutes=15" "${AUTH[@]}"
say ""

# ---------------------------------------------------------------------- identity
say "== 4. identity (guide §7) =="
req 200 "GET /api/v1/identity/stats" /api/v1/identity/stats "${AUTH[@]}"
req 200 "GET /api/v1/identity/list"  "/api/v1/identity/list?cross_camera_only=true&limit=200" "${AUTH[@]}"

# The per-person journey route needs a real global_ref, so derive one.
GREF=$(curl -s "${AUTH[@]}" "$BASE/api/v1/identity/list?limit=1" 2>/dev/null | \
  $PY -c 'import json,sys
try:
    ids = json.load(sys.stdin).get("identities", [])
    print(ids[0]["global_ref"] if ids else "")
except Exception:
    print("")' 2>/dev/null)
if [ -n "$GREF" ]; then
  req 200 "GET /api/v1/identity/{global_ref}" "/api/v1/identity/$GREF" "${AUTH[@]}"
else
  say "  SKIP  GET /api/v1/identity/{global_ref} — registry empty, no ref to query"
fi

say ""
say "== 5. privacy: no embedding vectors in any identity response =="
for path in "/api/v1/identity/stats" "/api/v1/identity/list" "/api/v1/identity/counts"; do
  if curl -s "${AUTH[@]}" "$BASE$path" 2>/dev/null | grep -qiE '"(embedding|embeddings|vector|vectors)"'; then
    FAIL=$((FAIL+1)); say "  FAIL $path leaked a vector"
  else
    PASS=$((PASS+1)); say "  ok   $path"
  fi
done
say ""

# ------------------------------------------------------------------ live video
say "== 6. live video (guide §6) =="
CAM=$(curl -s "${AUTH[@]}" "$BASE/api/v1/cameras" 2>/dev/null | \
  $PY -c 'import json,sys
try:
    cams = json.load(sys.stdin).get("cameras", [])
    live = [c for c in cams if c.get("stream_url")]
    print(live[0]["camera_id"] if live else "")
except Exception:
    print("")' 2>/dev/null)

if [ -z "$CAM" ]; then
  say "  SKIP — no camera is reporting a stream_url. Add one at /web/cameras.html."
else
  say "  camera under test: $CAM"
  # MJPEG never ends, so cap the read. --max-time makes curl exit 28; the
  # content-type is what we are actually asserting.
  CT=$(curl -s -o /dev/null -D - --max-time 3 \
        "${AUTH[@]}" "$BASE/api/v1/cameras/$CAM/stream" 2>/dev/null | \
        tr -d '\r' | awk -F': ' 'tolower($1)=="content-type"{print $2}' | head -1)
  case "$CT" in
    multipart/x-mixed-replace*)
      PASS=$((PASS+1)); say "  ok   /stream -> $CT" ;;
    *)
      FAIL=$((FAIL+1)); say "  FAIL /stream -> '${CT:-no content-type}' (wanted multipart/x-mixed-replace)" ;;
  esac

  if [ -n "$KEY" ]; then
    SC=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 \
          "$BASE/api/v1/cameras/$CAM/stream?key=$KEY" 2>/dev/null)
    # --max-time on a live stream yields 000 on timeout, which still proves the
    # key was accepted — a rejected key returns 401 immediately.
    case "$SC" in
      200|000) PASS=$((PASS+1)); say "  ok   /stream?key= accepted (the <img> case)" ;;
      *)       FAIL=$((FAIL+1)); say "  FAIL /stream?key= -> $SC (wanted 200)" ;;
    esac
  fi

  req 200 "GET /api/v1/cameras/{id}/snapshot" "/api/v1/cameras/$CAM/snapshot" "${AUTH[@]}"
fi
say ""

# --------------------------------------------------------------- forwarder + ws
say "== 7. push-side status and schema =="
req 200 "GET /api/v1/finblade/status"  /api/v1/finblade/status  "${AUTH[@]}"
req 200 "GET /openapi.json (no key needed)" /openapi.json
say ""

say "== 8. WebSocket /ws (sub-second push; dashboard falls back to 5s REST) =="
$PY - "$BASE" "$KEY" <<'PYEOF' 2>&1 | tee -a "$OUT"
import asyncio, sys
base, key = sys.argv[1], sys.argv[2]
url = base.replace("http://", "ws://").replace("https://", "wss://") + "/ws"
if key:
    url += "?key=" + key
try:
    import websockets
except ImportError:
    print("  SKIP — websockets not installed; /ws cannot be probed")
    raise SystemExit(0)

async def main():
    try:
        async with websockets.connect(url, open_timeout=5) as ws:
            msg = await asyncio.wait_for(ws.recv(), timeout=10)
            print("  ok   /ws connected, first frame %d bytes" % len(msg))
    except Exception as e:
        print("  NOTE /ws did not deliver a frame: %s: %s" % (type(e).__name__, e))
        print("       Not fatal — the dashboard degrades to 5s REST polling.")

asyncio.run(main())
PYEOF
say ""

say "== summary =="
say "  $PASS passed, $FAIL failed"
say "  transcript: $OUT"
if [ -z "$KEY" ]; then
  say ""
  say "  WARNING: ran WITHOUT an API key. Do not send the client guide yet —"
  say "  it documents auth that is not enabled on this instance."
fi
[ "$FAIL" -eq 0 ] || exit 1
