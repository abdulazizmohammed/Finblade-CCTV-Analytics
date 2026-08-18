#!/usr/bin/env bash
# Start, stop or check the dev Postgres — the one the API talks to.
#
#   bash scripts/pg_dev.sh start | stop | status
#
# WHY THIS EXISTS. The cluster in .pgdata was installed by pg_local_install.sh
# through the `pgserver` package, which is TEST tooling: it starts a postmaster
# while a Python process holds a handle and shuts it down when that process
# exits. Fine for a test run, useless as an application backend — the API's
# connection pool would die the moment the suite finished.
#
# So this starts the same bundled postmaster directly with pg_ctl, as a daemon
# that outlives every client. It listens on 127.0.0.1:5432 as well as the unix
# socket, because a TCP DSN is unambiguous and the unix-socket URI form is not:
# postgresql://postgres:@/finblade?host=/path silently resolves to the
# `postgres` database, not `finblade`, and you discover it when an empty
# database reports a million rows.
#
# NOT persistent across a WSL restart. Run `start` again after one, before
# start_stack.sh — with DATABASE_URL set the API connects eagerly and will
# refuse to boot against a dead server.
set -u
cd "$(dirname "$0")/.."

REPO="$PWD"
PGROOT="$REPO/.pgtest/pgserver/pginstall"
PGDATA="$REPO/.pgdata"
LOG="$REPO/scripts/logs/pg_dev.log"
export LD_LIBRARY_PATH="$PGROOT/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

if [ ! -x "$PGROOT/bin/pg_ctl" ]; then
  echo "no cluster binaries at $PGROOT"
  echo "run: bash scripts/pg_local_install.sh"
  exit 2
fi

mkdir -p "$(dirname "$LOG")"

case "${1:-status}" in
  start)
    if "$PGROOT/bin/pg_ctl" -D "$PGDATA" status >/dev/null 2>&1; then
      echo "already running"
    else
      "$PGROOT/bin/pg_ctl" -D "$PGDATA" -l "$LOG" \
        -o "-k $PGDATA -p 5432 -h 127.0.0.1" start
    fi
    ;;
  stop)
    "$PGROOT/bin/pg_ctl" -D "$PGDATA" stop
    ;;
  status)
    "$PGROOT/bin/pg_ctl" -D "$PGDATA" status || true
    ;;
  *)
    echo "usage: bash scripts/pg_dev.sh start|stop|status"
    exit 2
    ;;
esac

if [ "${1:-status}" != "stop" ]; then
  echo
  echo "DSN for .env:"
  echo "  DATABASE_URL=postgresql://postgres@127.0.0.1:5432/finblade"
  echo
  echo "Databases:"
  "$PGROOT/bin/psql" "postgresql://postgres@127.0.0.1:5432/postgres" -tAc \
    "SELECT '  ' || datname FROM pg_database WHERE datistemplate=false" 2>/dev/null \
    || echo "  (not reachable)"
fi
