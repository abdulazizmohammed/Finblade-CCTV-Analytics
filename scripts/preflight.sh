#!/usr/bin/env bash
# What is available before starting the stack.
cd "$(dirname "$0")/.." || exit 1
echo "== media =="
ls -la media/ 2>/dev/null | head -20 || echo "  (no media/ directory)"
echo
echo "== models =="
ls -la models/ 2>/dev/null | head -10
echo
echo "== configs =="
ls config/
echo
echo "== local key file =="
if [ -f .local_key ]; then cat .local_key; else echo "  (none)"; fi
echo
echo "== already running =="
pgrep -af 'uvicorn|run_cpu' || echo "  nothing"
echo
echo "== start scripts =="
ls scripts/*.sh | head -20
echo
echo "== python deps =="
.venv/bin/python -c "
import importlib.util as u
for m in ('fastapi','uvicorn','cv2','ultralytics','torch','flask','requests'):
    print('  %-12s %s' % (m, 'ok' if u.find_spec(m) else 'MISSING'))
" </dev/null
