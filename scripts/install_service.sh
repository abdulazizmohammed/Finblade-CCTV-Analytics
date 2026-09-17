#!/usr/bin/env bash
#
# Run the API as a systemd service, so it survives logout, reboot and crashes.
#
#   sudo bash scripts/install_service.sh
#
# Supply your own keys instead of generated ones:
#
#   sudo FINBLADE_INTEGRATION_KEY=<the key you gave FinBlade> \
#        bash scripts/install_service.sh
#
#   sudo FINBLADE_API_KEY=<operator key> FINBLADE_SITE_ID=SITE-02 \
#        bash scripts/install_service.sh
#
# Any FINBLADE_* value present in the environment is written into .env and takes
# effect on restart, whether .env already existed or not. Anything not supplied
# is generated on first install and left alone afterwards.
#
# Keys are deliberately NOT hard-coded in this file. It is committed to a public
# repository, so a literal key here would be published permanently and would
# stay in the history after any later removal. `sudo -E` does not carry them
# either — sudo strips the environment by default, which is why they are named
# on the command line above.
#
# Idempotent: safe to re-run to rotate a key or change the site id.
set -euo pipefail
cd "$(dirname "$0")/.." || exit 1
REPO="$(pwd -P)"

# The service must run as the user who owns the checkout and the venv, not root.
OWNER="$(stat -c '%U' "$REPO")"
UNIT=/etc/systemd/system/finblade-api.service

# FINBLADE_ENV_ONLY=1 writes .env and stops — no root, no systemd. Use it to set
# or rotate a key when the unit is already installed, then restart the service.
ENV_ONLY="${FINBLADE_ENV_ONLY:-0}"

[ "$ENV_ONLY" = "1" ] || [ "$(id -u)" -eq 0 ] || {
  echo "run with sudo: sudo bash scripts/install_service.sh" >&2
  echo "(or FINBLADE_ENV_ONLY=1 to only update .env)" >&2; exit 1; }
[ -x "$REPO/.venv/bin/python" ] || {
  echo "[BLOCKER] no venv at $REPO/.venv — run scripts/install_ubuntu.sh first" >&2
  exit 2; }

# ---------------------------------------------------------------- env file ---
GEN() { "$REPO/.venv/bin/python" -c 'import secrets;print(secrets.token_urlsafe(32))'; }

set_env() {   # set_env NAME VALUE — set exactly once, whatever the value contains
  local name="$1" value="$2" file="$REPO/.env" tmp
  tmp="$(mktemp "${file}.XXXX")"
  # Deliberately NOT sed. In a sed replacement '&' means the whole match and
  # '\' starts an escape, so a value containing either is silently corrupted —
  # a FINBLADE_URL can hold both, and the damage is invisible until the service
  # authenticates with a mangled secret. Dropping the old line and appending
  # the new one treats the value as literal text and cannot misfire.
  if [ -f "$file" ]; then
    grep -v "^${name}=" "$file" > "$tmp" || true
  fi
  printf '%s=%s\n' "$name" "$value" >> "$tmp"
  cat "$tmp" > "$file"          # keep the original inode, owner and mode
  rm -f "$tmp"
}

if [ ! -f "$REPO/.env" ]; then
  echo "== creating .env =="
  cat > "$REPO/.env" <<EOF
# FinBlade CCTV service configuration. Read by systemd; keep chmod 600.
# Operator key — full access, used by the dashboard.
FINBLADE_API_KEY=$(GEN)
# Scoped key for a platform integration: every GET plus /ws, and only the two
# alert-action writes. Give this one to FinBlade.
FINBLADE_INTEGRATION_KEY=$(GEN)

FINBLADE_SITE_ID=SITE-01
FINBLADE_PORT=8000
FINBLADE_SELF_URL=http://127.0.0.1:8000

# Relaunch camera pipelines on start. Without this an API restart leaves every
# camera row intact and every pipeline down, which looks like the cameras
# failed rather than like nothing started them.
FINBLADE_AUTOSTART_CAMERAS=1

# Event bus. Two streams: fb:events (every ingested event) and fb:facility
# (merged occupancy counts). Unset, the API runs on an in-process bus and
# nothing outside it ever sees a count — a working system with no bus.
# Check /api/v1/health -> checks.facility_counts.bus to see which is in use.
REDIS_URL=redis://127.0.0.1:6379/0

# Push to FinBlade (optional): set the URL to enable.
# FINBLADE_URL=https://finblade.example.com
# FINBLADE_OUTBOUND_KEY=
EOF
  chown "$OWNER" "$REPO/.env"
  chmod 600 "$REPO/.env"
else
  echo "== .env exists, keeping it =="
  grep -q FINBLADE_PORT "$REPO/.env" || echo "FINBLADE_PORT=8000" >> "$REPO/.env"
  grep -q FINBLADE_AUTOSTART_CAMERAS "$REPO/.env" \
    || echo "FINBLADE_AUTOSTART_CAMERAS=1" >> "$REPO/.env"
  grep -q REDIS_URL "$REPO/.env" \
    || echo "REDIS_URL=redis://127.0.0.1:6379/0" >> "$REPO/.env"
  # An .env that predates key generation has NO keys in it, and this branch
  # does not add them — so auth is off and nothing says so. Generating them
  # here would turn auth on under an operator who never asked, so say it
  # instead and let them run the documented rotation.
  grep -q FINBLADE_API_KEY "$REPO/.env" || {
    echo "!! .env has no FINBLADE_API_KEY — the API is UNAUTHENTICATED." >&2
    echo "   To enable auth:  FINBLADE_API_KEY=\$(.venv/bin/python -c \\" >&2
    echo "       'import secrets;print(secrets.token_urlsafe(32))') \\" >&2
    echo "       bash scripts/install_service.sh" >&2
  }
fi

# Anything supplied in the environment wins, on a fresh install or an existing
# one. This is how you set the key you have already handed to FinBlade, and how
# you rotate it later without hand-editing .env.
for var in FINBLADE_API_KEY FINBLADE_INTEGRATION_KEY FINBLADE_SITE_ID \
           FINBLADE_PORT FINBLADE_URL FINBLADE_OUTBOUND_KEY \
           FINBLADE_AUTOSTART_CAMERAS FINBLADE_STREAM_HOST \
           FINBLADE_MCP_TOKEN FINBLADE_MCP_PORT FINBLADE_MCP_SEARCH_KEY; do
  value="${!var-}"
  if [ -n "$value" ]; then
    set_env "$var" "$value"
    case "$var" in
      *KEY) echo "  set $var (from the environment)" ;;
      *)    echo "  set $var=$value" ;;
    esac
  fi
done
chown "$OWNER" "$REPO/.env" 2>/dev/null || true
chmod 600 "$REPO/.env"

if [ "$ENV_ONLY" = "1" ]; then
  echo
  echo ".env updated. It is read at process start, so apply it with:"
  echo "  sudo systemctl restart finblade-api"
  exit 0
fi

# ------------------------------------------------------------------- unit ----
echo "== installing $UNIT =="
sed -e "s|__REPO__|$REPO|g" -e "s|__USER__|$OWNER|g" \
    "$REPO/deploy/finblade-api.service" > "$UNIT"
chmod 644 "$UNIT"

systemctl daemon-reload
systemctl enable finblade-api >/dev/null
systemctl restart finblade-api

sleep 3
echo
systemctl --no-pager --lines=0 status finblade-api || true

# ---------------------------------------------------------------- MCP unit ---
# The chatbot's MCP server (docs/MCP.md) is installed only once .env carries
# FINBLADE_MCP_TOKEN: without a token the endpoint is open, and a unit that
# starts an open endpoint on boot is not something to install by accident.
MCP_UNIT=/etc/systemd/system/finblade-mcp.service
if grep -q '^FINBLADE_MCP_TOKEN=.\+' "$REPO/.env"; then
  grep -q '^FINBLADE_MCP_PORT=' "$REPO/.env" || echo "FINBLADE_MCP_PORT=8010" >> "$REPO/.env"
  echo "== installing $MCP_UNIT =="
  sed -e "s|__REPO__|$REPO|g" -e "s|__USER__|$OWNER|g" \
      "$REPO/deploy/finblade-mcp.service" > "$MCP_UNIT"
  chmod 644 "$MCP_UNIT"
  # The nohup copy from scripts/start_mcp.sh, if one is running, holds the port.
  pkill -u "$OWNER" -f 'services.mcp.server' 2>/dev/null || true
  systemctl daemon-reload
  systemctl enable finblade-mcp >/dev/null
  systemctl restart finblade-mcp
  sleep 2
  systemctl --no-pager --lines=0 status finblade-mcp || true
  MCP_LINE="  mcp       sudo systemctl status finblade-mcp   (journalctl -u finblade-mcp -f)"
else
  MCP_LINE="  mcp       not installed: no FINBLADE_MCP_TOKEN in .env (see docs/MCP.md)"
fi

cat <<EOF

Installed and running as $OWNER, from $REPO.

  status    sudo systemctl status finblade-api
  logs      sudo journalctl -u finblade-api -f
  restart   sudo systemctl restart finblade-api
  stop      sudo systemctl stop finblade-api
  disable   sudo systemctl disable --now finblade-api
$MCP_LINE

It now starts on boot and restarts on crash, and survives you closing SSH.

Your operator key (paste into the dashboard):
  grep FINBLADE_API_KEY $REPO/.env

The key to give the FinBlade team:
  grep FINBLADE_INTEGRATION_KEY $REPO/.env
EOF
