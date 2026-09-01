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
#
# THE PORT IS NOT ASSUMED, IT IS READ BACK. This script used to print a 5432 DSN
# unconditionally, and probe 5432 to list the databases, without ever checking
# that the cluster in .pgdata was the thing answering there. On a box that also
# runs an apt-installed Postgres — which owns 5432 and is started by systemd at
# boot, long before this runs — the .pgdata cluster ends up on another port and
# every 5432 default in the repo silently reaches the SYSTEM server instead.
#
# That failure is nasty because it does not look like a port problem. The system
# cluster refuses a passwordless TCP connection (Debian's pg_hba allows peer on
# the unix socket only), so the whole test suite fails with
# "fe_sendauth: no password supplied" — an auth error, pointing at a database
# that has nothing to do with this project, while the real one sits there with
# `trust` in its pg_hba and nobody talking to it.
set -u
cd "$(dirname "$0")/.."

REPO="$PWD"
PGROOT="$REPO/.pgtest/pgserver/pginstall"
PGDATA="$REPO/.pgdata"
LOG="$REPO/scripts/logs/pg_dev.log"
# PGPORT overrides, for a box where 5432 cannot be freed. Deliberately explicit:
# the script will not quietly choose another port for you, but it will use one
# you name, and every DSN it prints afterwards carries it.
WANT_PORT="${PGPORT:-5432}"
export LD_LIBRARY_PATH="$PGROOT/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
# Never let libpq block on an interactive password prompt. Probing a cluster
# that wants a password used to hang this script forever with no output, since
# the prompt goes to the terminal and the 2>/dev/null hid nothing useful.
export PGCONNECT_TIMEOUT=5

if [ ! -x "$PGROOT/bin/pg_ctl" ]; then
  echo "no cluster binaries at $PGROOT"
  echo "run: bash scripts/pg_local_install.sh"
  exit 2
fi

mkdir -p "$(dirname "$LOG")"

# Line 4 of postmaster.pid is the port the running postmaster actually bound.
# Authoritative, and it costs nothing — unlike asking the server, which requires
# connecting to it, which requires already knowing the port.
pgdata_port() {
  [ -f "$PGDATA/postmaster.pid" ] || return 1
  awk 'NR==4 {gsub(/[[:space:]]/, ""); print; exit}' "$PGDATA/postmaster.pid"
}

# Whatever is already listening on a TCP port, for the message when it is not us.
port_holder() {
  ss -ltnp 2>/dev/null | awk -v pat=":$1\$" '$4 ~ pat {print; exit}'
}

case "${1:-status}" in
  start)
    if "$PGROOT/bin/pg_ctl" -D "$PGDATA" status >/dev/null 2>&1; then
      echo "already running on port $(pgdata_port)"
    else
      # Refuse to start rather than land somewhere unexpected. Postgres does not
      # fall back to another port on its own — it fails to bind — but the
      # pgserver test tooling that also drives this data directory DOES pick a
      # free one, and either way a cluster on a port nothing in the repo expects
      # is worse than a cluster that did not start and said why.
      holder="$(port_holder "$WANT_PORT")"
      if [ -n "$holder" ]; then
        echo "port $WANT_PORT is already taken, and not by this cluster:"
        echo "  $holder"
        echo
        echo "That is almost certainly the apt-installed Postgres started by"
        echo "systemd at boot. It is a DIFFERENT server with a different"
        echo "pg_hba.conf, and every 5432 default in this repo would reach it"
        echo "instead of .pgdata. Free the port, then start again:"
        echo "  sudo systemctl disable --now postgresql"
        echo
        echo "Or run this cluster elsewhere, and export the matching DSNs:"
        echo "  PGPORT=5433 bash scripts/pg_dev.sh start"
        exit 3
      fi
      "$PGROOT/bin/pg_ctl" -D "$PGDATA" -l "$LOG" \
        -o "-k $PGDATA -p $WANT_PORT -h 127.0.0.1" start
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
  PORT="$(pgdata_port || true)"
  if [ -z "${PORT:-}" ]; then
    echo
    echo "  (cluster not running — nothing to connect to)"
    exit 0
  fi

  # Prove the server on that port IS this data directory before printing a DSN
  # for it. -w so a cluster wanting a password fails instead of prompting.
  actual_dir="$("$PGROOT/bin/psql" -w \
      "postgresql://postgres@127.0.0.1:$PORT/postgres" \
      -tAc "SHOW data_directory" 2>/dev/null || true)"

  echo
  if [ "$actual_dir" != "$PGDATA" ]; then
    echo "!! port $PORT does not answer as $PGDATA"
    echo "!! it said: ${actual_dir:-<unreachable>}"
    echo "!! Something else is on that port. Do not trust the DSN below."
  elif [ "$PORT" != "$WANT_PORT" ]; then
    echo "!! This cluster is on port $PORT, NOT the $WANT_PORT that"
    echo "!! tests/pgfixture.py and the docs assume. Anything using the"
    echo "!! default will reach a different server and fail with"
    echo "!! 'fe_sendauth: no password supplied'. Until the port is freed:"
    echo "!!   export FINBLADE_TEST_DSN=postgresql://postgres@127.0.0.1:$PORT/postgres"
    echo
  fi

  echo "DSN for .env:"
  echo "  DATABASE_URL=postgresql://postgres@127.0.0.1:$PORT/finblade"
  echo
  echo "Databases:"
  "$PGROOT/bin/psql" -w "postgresql://postgres@127.0.0.1:$PORT/postgres" -tAc \
    "SELECT '  ' || datname FROM pg_database WHERE datistemplate=false" 2>/dev/null \
    || echo "  (not reachable)"
fi
