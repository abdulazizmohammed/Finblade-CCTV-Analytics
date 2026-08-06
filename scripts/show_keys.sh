#!/usr/bin/env bash
# Which key does the RUNNING API actually accept? Prints it and proves it works.
#
#   bash scripts/show_keys.sh
#
# The key files on disk and the key the API is enforcing can differ: the API
# reads FINBLADE_API_KEY from the environment it was STARTED with, so a process
# launched before the file was written, or from another shell, is using
# something else — or nothing at all.
cd "$(dirname "$0")/.." || exit 1
PORT="${PORT:-8000}"

echo "== key files on disk =="
for f in .local_key .local_integration_key; do
  if [ -f "$f" ]; then
    printf '  %-24s %s\n' "$f" "$(cat "$f")"
  else
    printf '  %-24s (not created yet)\n' "$f"
  fi
done

echo
echo "== key the running API was started with =="
PID=$(pgrep -f 'uvicorn services.api.app' | head -1)
if [ -z "$PID" ]; then
  echo "  API is not running — start it first"
else
  echo "  pid $PID"
  ENVV=$(tr '\0' '\n' < "/proc/$PID/environ" 2>/dev/null | grep '^FINBLADE_' || true)
  if [ -z "$ENVV" ]; then
    echo "  no FINBLADE_* variables in its environment"
    echo "  => AUTHENTICATION IS OFF. The UI will not ask for a key, and"
    echo "     anyone who can reach port $PORT has full access."
  else
    echo "$ENVV" | sed 's/^/    /'
  fi
fi

echo
echo "== what the API says =="
CODE=$(curl -s -o /dev/null -m 10 -w '%{http_code}' "localhost:$PORT/api/v1/cameras")
if [ "$CODE" = "200" ]; then
  echo "  no key needed (auth disabled) — HTTP 200 without credentials"
elif [ "$CODE" = "401" ]; then
  echo "  auth is ON (401 without a key). Testing the files:"
  # Both keys pass a GET, so a read test alone cannot tell them apart — and
  # putting the scoped key into the UI produces a dashboard that loads fine and
  # then 403s the moment anyone saves a zone or adds a camera. The write probe
  # below is a POST to a route only the operator key may use; it is sent with a
  # deliberately empty body so it can never succeed and change anything.
  for f in .local_key .local_integration_key; do
    [ -f "$f" ] || continue
    K=$(cat "$f")
    R=$(curl -s -o /dev/null -m 10 -w '%{http_code}' \
          -H "Authorization: Bearer $K" "localhost:$PORT/api/v1/cameras")
    W=$(curl -s -o /dev/null -m 10 -w '%{http_code}' -X POST \
          -H "Authorization: Bearer $K" -H 'Content-Type: application/json' \
          -d '{}' "localhost:$PORT/api/v1/zones")
    if [ "$R" != "200" ]; then
      printf '    %-24s rejected (HTTP %s) — not this API'"'"'s key\n' "$f" "$R"
    elif [ "$W" = "403" ]; then
      printf '    %-24s scoped: reads + alert ack only. NOT for the UI.\n' "$f"
    else
      printf '    %-24s OPERATOR  <- paste this into the dashboard\n' "$f"
      printf '    %-24s %s\n' "" "$K"
    fi
  done
else
  echo "  unexpected HTTP $CODE — is the API up? bash scripts/status_demo.sh"
fi

cat <<EOF

Log in:  open http://<host>:$PORT/web/dashboard.html
         it prompts once and stores the key in the browser's localStorage.

Wrong key stored?  In the browser console:
         localStorage.removeItem('finblade_api_key'); location.reload()

Lost the key entirely?  It is not recoverable — it is only ever compared, never
stored by the API. Restart with a new one:
         bash scripts/stop_demo.sh
         rm .local_key .local_integration_key
         bash scripts/start_demo.sh
EOF
