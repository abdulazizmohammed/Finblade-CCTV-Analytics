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
fi

# Anything supplied in the environment wins, on a fresh install or an existing
# one. This is how you set the key you have already handed to FinBlade, and how
# you rotate it later without hand-editing .env.
for var in FINBLADE_API_KEY FINBLADE_INTEGRATION_KEY FINBLADE_SITE_ID \
           FINBLADE_PORT FINBLADE_URL FINBLADE_OUTBOUND_KEY \
           FINBLADE_AUTOSTART_CAMERAS FINBLADE_STREAM_HOST; do
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

cat <<EOF

Installed and running as $OWNER, from $REPO.

  status    sudo systemctl status finblade-api
  logs      sudo journalctl -u finblade-api -f
  restart   sudo systemctl restart finblade-api
  stop      sudo systemctl stop finblade-api
  disable   sudo systemctl disable --now finblade-api

It now starts on boot and restarts on crash, and survives you closing SSH.

Your operator key (paste into the dashboard):
  grep FINBLADE_API_KEY $REPO/.env

The key to give the FinBlade team:
  grep FINBLADE_INTEGRATION_KEY $REPO/.env
EOF
