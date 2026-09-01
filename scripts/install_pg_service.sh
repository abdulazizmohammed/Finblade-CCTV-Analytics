#!/usr/bin/env bash
# Install the dev Postgres cluster as a systemd service, so it comes back after
# a WSL restart.
#
#   sudo bash scripts/install_pg_service.sh
#
# WHY. The cluster in .pgdata is started by pg_ctl, not by a package, so it was
# gone after every reboot — and because the API connects eagerly when
# DATABASE_URL is set, a dead database looked like a broken application. This
# installs deploy/finblade-postgres.service, which starts it on boot.
#
# Separate from install_service.sh because that one owns the API: it generates
# keys, writes .env and installs finblade-api.service. This owns one thing, and
# a box with a packaged or remote Postgres should never run it.
#
# NOT for production. Deploy a real packaged Postgres there and point
# DATABASE_URL at it; this exists because the dev box's cluster came from test
# tooling that was never meant to outlive a Python process.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
UNIT=/etc/systemd/system/finblade-postgres.service
TEMPLATE="$REPO/deploy/finblade-postgres.service"
PGDATA="$REPO/.pgdata"
PGROOT="$REPO/.pgtest/pgserver/pginstall"

[ "$(id -u)" = "0" ] || { echo "run with sudo" >&2; exit 1; }

# The unit runs as whoever owns the data directory. Postgres refuses to start
# otherwise, and guessing $SUDO_USER would be wrong on a box where the cluster
# was created by someone else.
OWNER="$(stat -c %U "$PGDATA" 2>/dev/null || true)"
[ -n "$OWNER" ] || { echo "no data directory at $PGDATA" >&2
                     echo "run: bash scripts/pg_local_install.sh" >&2; exit 2; }
[ -x "$PGROOT/bin/pg_ctl" ] || { echo "no pg_ctl at $PGROOT/bin" >&2; exit 2; }
[ -f "$TEMPLATE" ] || { echo "missing $TEMPLATE" >&2; exit 2; }

# Refuse to install a second server onto a port something else already holds —
# the same guard scripts/pg_dev.sh applies, and for the same reason: two
# clusters on one box is what produced a whole afternoon of
# "fe_sendauth: no password supplied" against a database nobody was using.
holder="$(ss -ltnp 2>/dev/null | awk '$4 ~ /:5432$/ {print; exit}' || true)"
running_here=0
if [ -f "$PGDATA/postmaster.pid" ] \
   && [ "$(awk 'NR==4 {gsub(/[[:space:]]/,""); print}' "$PGDATA/postmaster.pid")" = "5432" ]; then
  running_here=1
fi
if [ -n "$holder" ] && [ "$running_here" = "0" ]; then
  echo "port 5432 is held by something that is not this cluster:" >&2
  echo "  $holder" >&2
  echo >&2
  echo "Almost certainly the apt-installed Postgres. Free it first:" >&2
  echo "  sudo systemctl disable --now postgresql" >&2
  exit 3
fi

mkdir -p "$REPO/scripts/logs"
chown "$OWNER" "$REPO/scripts/logs" 2>/dev/null || true

echo "== installing $UNIT (user $OWNER, repo $REPO) =="
sed -e "s|__REPO__|$REPO|g" -e "s|__USER__|$OWNER|g" "$TEMPLATE" > "$UNIT"
chmod 644 "$UNIT"

systemctl daemon-reload
systemctl enable finblade-postgres >/dev/null

# Adopt a cluster that is already up rather than restarting it: pg_ctl start on
# a running server fails, and bouncing a database nobody asked us to bounce is
# not what "install a unit" should mean.
if systemctl is-active --quiet finblade-postgres; then
  echo "already running under systemd"
elif [ "$running_here" = "1" ]; then
  echo "cluster is already up outside systemd — restarting it under the unit"
  sudo -u "$OWNER" env LD_LIBRARY_PATH="$PGROOT/lib" \
    "$PGROOT/bin/pg_ctl" -D "$PGDATA" -m fast -w -t 60 stop
  systemctl start finblade-postgres
else
  systemctl start finblade-postgres
fi

sleep 2
echo
systemctl --no-pager --lines=0 status finblade-postgres || true

cat <<EOF

Installed. The cluster now starts on boot and restarts on failure.

  status    sudo systemctl status finblade-postgres
  logs      sudo journalctl -u finblade-postgres -f
  restart   sudo systemctl restart finblade-postgres
  disable   sudo systemctl disable --now finblade-postgres

scripts/pg_dev.sh still works for ad-hoc use, but prefer systemctl now — a
cluster started by hand is not the one systemd will try to manage on the next
reboot.
EOF
