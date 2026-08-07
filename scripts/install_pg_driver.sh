#!/usr/bin/env bash
# Add the Postgres driver to the application venv.
#
# A real dependency now, not test tooling: the app is moving to Postgres, so
# psycopg belongs in requirements.txt alongside fastapi. Installed with
# -c constraints.txt like everything else — DEPLOY.md is emphatic that a stray
# install which drags numpy forward takes the vision stack down at runtime
# rather than at install time.
#
# psycopg[pool] as well as [binary]: a connection pool is the point of moving
# off SQLite. A single locked connection would give up the concurrency the
# migration is for.
set -u
cd "$(dirname "$0")/.."
mkdir -p scripts/logs
exec > >(tee -a scripts/logs/pg_driver.log) 2>&1
echo "=== $(date -Is)"

source .venv/bin/activate

echo "== numpy before (the canary: it must not move)"
python -c "import numpy; print('  numpy', numpy.__version__)"

pip install -c constraints.txt "psycopg[binary,pool]" 2>&1 | tail -4

echo
echo "== installed"
python - <<'PY'
import psycopg, psycopg_pool
print("  psycopg     ", psycopg.__version__)
print("  psycopg_pool", psycopg_pool.__version__)
PY

echo
echo "== numpy after"
python -c "import numpy; print('  numpy', numpy.__version__)"
echo "== torch still imports"
python -c "import torch; print('  torch', torch.__version__)" 2>&1 | tail -1
