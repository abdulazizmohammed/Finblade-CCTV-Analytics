#!/usr/bin/env bash
cd /home/usv/finblade-cctv
for p in $(pgrep -f 'run_cpu.py|uvicorn services.api'); do kill -9 "$p" 2>/dev/null; done
sleep 1; rm -f data/finblade.db
.venv/bin/python -m uvicorn services.api.app:app --host 0.0.0.0 --port 8000 --log-level warning > scripts/api.log 2>&1 &
sleep 4
.venv/bin/python -u services/inference/run_cpu.py --config config/cameras.synthetic.yaml \
  --seconds 30 --port 8090 --stream-host 127.0.0.1 --api-url http://127.0.0.1:8000 > scripts/p8.log 2>&1 &
RUNNER=$!
sleep 9
echo "=== cameras after 9s (expect ONLINE + fps + resolution + stream_url) ==="
.venv/bin/python - <<'PY'
import json, urllib.request
c=json.loads(urllib.request.urlopen("http://127.0.0.1:8000/api/v1/cameras",timeout=3).read())["cameras"]
for x in c:
    print(f"  {x['camera_id']}: state={x['effective_state']} fps={x.get('input_fps')} "
          f"res={x.get('resolution')} online={x['online']} sim={x.get('sim_failure')}")
    print(f"    stream_url={x.get('stream_url')}")
PY
echo "=== POST simulate-failure (central control) ==="
.venv/bin/python - <<'PY'
import urllib.request
r=urllib.request.urlopen(urllib.request.Request(
    "http://127.0.0.1:8000/api/v1/cameras/CAM-SYN-01/simulate-failure", method="POST"), timeout=3)
print("  ->", r.read().decode())
PY
sleep 8
echo "=== runner should have applied the failover ==="
grep -iE 'SIMULATED failure' scripts/p8.log | tail -1 || echo "  (not found)"
.venv/bin/python - <<'PY'
import json, urllib.request
c=json.loads(urllib.request.urlopen("http://127.0.0.1:8000/api/v1/cameras",timeout=3).read())["cameras"]
for x in c:
    print(f"  {x['camera_id']}: state={x['effective_state']} sim={x.get('sim_failure')}")
PY
echo "--- errors ---"; grep -iE 'error|traceback' scripts/p8.log | head -3 || true
kill -9 $RUNNER 2>/dev/null
for p in $(pgrep -f 'run_cpu.py|uvicorn services.api'); do kill -9 "$p" 2>/dev/null; done
