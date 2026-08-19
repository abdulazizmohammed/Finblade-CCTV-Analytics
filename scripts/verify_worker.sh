#!/usr/bin/env bash
cd /home/usv/finblade-cctv
for p in $(pgrep -f 'run_cpu.py'); do kill -9 "$p" 2>/dev/null; done
# short offline threshold for a quick live check (spec default is 30s)
.venv/bin/python - <<'PY'
import yaml
d = yaml.safe_load(open("config/cameras.synthetic.yaml"))
d["offline_seconds"] = 8
d["process_fps"] = 15
yaml.safe_dump(d, open("config/_worker_test.yaml", "w"))
print("wrote _worker_test.yaml (offline_seconds=8)")
PY
FB_LOG_LEVEL=INFO .venv/bin/python -u services/inference/run_cpu.py \
  --config config/_worker_test.yaml --seconds 45 > scripts/worker.log 2>&1 &
sleep 16

check() {  # hit an endpoint on the runner's :8080 control server
  .venv/bin/python - "$1" <<'PY'
import sys, json, urllib.request
try:
    r = urllib.request.urlopen("http://127.0.0.1:8080"+sys.argv[1], timeout=3)
    print(sys.argv[1], "->", r.read().decode())
except Exception as e:
    print(sys.argv[1], "ERR", e)
PY
}

echo "=== health (expect ONLINE, input_fps>0) ==="; check /health
echo "=== simulate failure ==="; check /simulate-failure
sleep 11
echo "=== health after 11s no frames (expect OFFLINE) ==="; check /health
echo "=== restore ==="; check /restore
sleep 4
echo "=== health after restore (expect ONLINE) ==="; check /health
echo "=== state transitions logged ==="; grep 'state ->' scripts/worker.log | sed 's/(.*//'
echo "=== errors? ==="; grep -iE 'error|traceback' scripts/worker.log | head -3 || true
for p in $(pgrep -f 'run_cpu.py'); do kill -9 "$p" 2>/dev/null; done
rm -f config/_worker_test.yaml
