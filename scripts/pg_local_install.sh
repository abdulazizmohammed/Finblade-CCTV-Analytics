#!/usr/bin/env bash
# Install a PostgreSQL server we can test against, WITHOUT root.
#
# apt needs sudo and there is no password here, but Postgres does not actually
# need root — it refuses to run AS root — and a cluster can live entirely in a
# directory this user owns. `pgserver` on PyPI ships the official binaries in a
# wheel, which is the shortest route to a real server on this box.
#
# THREE THINGS THIS SCRIPT WORKS AROUND, all found the hard way:
#
#   1. /tmp is volatile. This WSL distro shuts down when idle and restarts on
#      the next command, wiping /tmp. Logs written there came back empty and
#      looked like hung jobs. Everything logs into the repo instead.
#   2. `python3 -m venv` fails — python3.10-venv is not installed and needs
#      root. So this installs with `pip --target` into a plain directory and
#      puts it on PYTHONPATH. No venv required.
#   3. Without --only-binary, pip will build PostgreSQL FROM SOURCE. That
#      pegged the box for half an hour on the first attempt. If there is no
#      wheel for this platform we want to fail in seconds, not after a compile.
#
# The application venv is deliberately untouched. This is test tooling; the
# app's pinned dependencies do not change because we wanted a database.
set -u
cd "$(dirname "$0")/.."

PGLIB="${PGLIB:-$(pwd)/.pgtest}"
PGDATA="${PGDATA:-$(pwd)/.pgdata}"
mkdir -p scripts/logs "${PGLIB}"
exec > >(tee -a scripts/logs/pg_install.log) 2>&1
echo "=== $(date -Is) starting"

PIP="$(pwd)/.venv/bin/pip"
PY="$(pwd)/.venv/bin/python"
[ -x "${PIP}" ] || { echo "no pip at ${PIP}"; exit 1; }

echo "== installing into ${PGLIB} (app venv untouched)"
for pkg in "pgserver" "psycopg[binary]"; do
  echo "-- ${pkg}"
  "${PIP}" install --only-binary=:all: --target "${PGLIB}" --upgrade "${pkg}" 2>&1 | tail -2
done

export PYTHONPATH="${PGLIB}"
echo
echo "== what landed"
"${PY}" - <<'PY'
import importlib
for name in ("pgserver", "psycopg"):
    try:
        m = importlib.import_module(name)
        print(f"  {name}: {getattr(m, '__version__', 'present')} @ {getattr(m, '__file__', '?')}")
    except Exception as exc:
        print(f"  {name}: FAILED {exc.__class__.__name__}: {exc}")
PY

echo
echo "== starting a cluster in ${PGDATA}"
mkdir -p "${PGDATA}"
"${PY}" - "${PGDATA}" <<'PY'
import sys
try:
    import pgserver
except ImportError as exc:
    print(f"  cannot import pgserver: {exc}")
    raise SystemExit(3)
srv = pgserver.get_server(sys.argv[1])
uri = srv.get_uri()
print("  server up")
print(f"  DSN: {uri}")
with open("scripts/logs/pg_dsn.txt", "w") as fh:
    fh.write(uri + "\n")
print("  (DSN written to scripts/logs/pg_dsn.txt)")
PY
rc=$?
echo "=== $(date -Is) done rc=${rc}"
exit "${rc}"
