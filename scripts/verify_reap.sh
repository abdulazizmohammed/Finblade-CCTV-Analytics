#!/usr/bin/env bash
cd /home/usv/finblade-cctv
for p in $(pgrep -f 'run_cpu.py'); do kill -9 "$p" 2>/dev/null; done
export FB_LOG_LEVEL=DEBUG
.venv/bin/python -u services/inference/run_cpu.py --config config/cameras.synthetic.yaml \
  --seconds 20 --no-serve > scripts/reap.log 2>&1
echo "=== eviction log lines (finblade.inference DEBUG) ==="
grep 'evicted' scripts/reap.log | head -5
echo "total eviction events: $(grep -c 'evicted' scripts/reap.log)"
echo "=== max 'active=' reported (should stay small/bounded) ==="
grep -oE 'active=[0-9]+' scripts/reap.log | sort -t= -k2 -n | tail -1
echo "=== errors ==="
grep -iE 'error|traceback' scripts/reap.log | head -3 || true
