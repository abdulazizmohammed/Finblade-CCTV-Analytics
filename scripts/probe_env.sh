#!/usr/bin/env bash
# Environment probe — safe, read-only.
echo "=== whoami/pwd ==="
whoami; pwd
echo "=== python ==="
which python3; python3 --version 2>&1
echo "=== venvs ==="
ls -d /home/usv/finblade-cctv/.venv 2>&1
echo "=== pip packages of interest ==="
for pkg in ultralytics cv2 torch numpy pytest fastapi pydantic redis psycopg2 uvicorn yaml shapely; do
  python3 - "$pkg" <<'PY' 2>&1
import importlib, sys
name = sys.argv[1]
try:
    m = importlib.import_module(name)
    v = getattr(m, "__version__", getattr(m, "VERSION", "?"))
    print(f"  OK   {name} = {v}")
except Exception as e:
    print(f"  MISS {name}: {type(e).__name__}")
PY
done
echo "=== media/ ==="
ls -la /home/usv/finblade-cctv/media/ 2>&1
echo "=== models/ ==="
ls -la /home/usv/finblade-cctv/models/ 2>&1 || echo "no models dir"
echo "=== docker ==="
which docker 2>&1; docker --version 2>&1 | head -1
echo "=== redis-server / psql ==="
which redis-server 2>&1; which psql 2>&1
echo "=== done ==="
