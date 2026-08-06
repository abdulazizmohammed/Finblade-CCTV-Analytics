#!/usr/bin/env bash
#
# Run the API as a systemd service, so it survives logout, reboot and crashes.
#
#   sudo bash scripts/install_service.sh
#
# Idempotent. Creates .env (chmod 600) with generated keys if it does not exist,
# and never overwrites one that does.
set -euo pipefail
cd "$(dirname "$0")/.." || exit 1
REPO="$(pwd -P)"

# The service must run as the user who owns the checkout and the venv, not root.
OWNER="$(stat -c '%U' "$REPO")"
UNIT=/etc/systemd/system/finblade-api.service

[ "$(id -u)" -eq 0 ] || { echo "run with sudo: sudo bash scripts/install_service.sh" >&2; exit 1; }
[ -x "$REPO/.venv/bin/python" ] || {
  echo "[BLOCKER] no venv at $REPO/.venv — run scripts/install_ubuntu.sh first" >&2
  exit 2; }

# ---------------------------------------------------------------- env file ---
if [ ! -f "$REPO/.env" ]; then
  echo "== creating .env with fresh keys =="
  GEN() { "$REPO/.venv/bin/python" -c 'import secrets;print(secrets.token_urlsafe(32))'; }
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
  echo "== .env already exists, leaving it alone =="
  grep -q FINBLADE_PORT "$REPO/.env" || echo "FINBLADE_PORT=8000" >> "$REPO/.env"
  grep -q FINBLADE_AUTOSTART_CAMERAS "$REPO/.env" \
    || echo "FINBLADE_AUTOSTART_CAMERAS=1" >> "$REPO/.env"
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
