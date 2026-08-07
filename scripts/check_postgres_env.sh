#!/usr/bin/env bash
# Can this machine run and TEST a Postgres migration?
#
# Asked before writing any of it. An untested store is worse than no store:
# it looks finished, and the first thing it does in production is fail on the
# method nobody exercised.
set -u
cd "$(dirname "$0")/.."

echo "== server binaries on PATH"
found=0
for b in postgres psql pg_ctl initdb pg_dump mysqld docker podman; do
  p=$(command -v "$b" 2>/dev/null) && { echo "  $b -> $p"; found=1; }
done
[ "$found" -eq 0 ] && echo "  none"

echo
echo "== packaged postgres"
ls -d /usr/lib/postgresql/* 2>/dev/null || echo "  /usr/lib/postgresql absent"
dpkg -l 2>/dev/null | grep -iE "postgresql|mysql-server" | head -5 || true

echo
echo "== python drivers in the venv"
source .venv/bin/activate 2>/dev/null
for mod in psycopg2 psycopg asyncpg pg8000 sqlalchemy pymysql; do
  python - "$mod" <<'PY'
import importlib, sys
name = sys.argv[1]
try:
    m = importlib.import_module(name)
    print(f"  {name}: {getattr(m, '__version__', 'present')}")
except Exception as exc:
    print(f"  {name}: MISSING ({exc.__class__.__name__})")
PY
done

echo
echo "== can we install anything?"
if timeout 8 python -c "import urllib.request;urllib.request.urlopen('https://pypi.org/simple/',timeout=6)" 2>/dev/null; then
  echo "  pypi reachable"
else
  echo "  pypi NOT reachable — no pip install (CLAUDE.md: assume no network)"
fi
if timeout 8 bash -c 'exec 3<>/dev/tcp/archive.ubuntu.com/80' 2>/dev/null; then
  echo "  apt mirror reachable"
else
  echo "  apt mirror NOT reachable — no apt install"
fi

echo
echo "== is a server already listening anywhere?"
ss -ltnp 2>/dev/null | grep -E ':(5432|3306)\b' || echo "  nothing on 5432 or 3306"
