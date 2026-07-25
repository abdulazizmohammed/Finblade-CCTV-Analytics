#!/usr/bin/env bash
cd /home/usv/finblade-cctv
for p in $(pgrep -f run_cpu.py); do kill -9 "$p" 2>/dev/null; done
.venv/bin/python -u services/inference/run_cpu.py --config config/cameras.synthetic.yaml \
  --seconds 25 --no-serve > scripts/p2.log 2>&1
echo "RC=$?"
echo "=== metrics (track counts + detection) ==="
grep -E 'device|avg_fps|active_tracks|completed_tracks|unique_track|"avg"|"max"' \
  evidence/metrics_CAM-SYN-01.json
echo "=== detection ran (tracked lines): $(grep -c tracked= scripts/p2.log) ==="
grep -iE 'error|traceback' scripts/p2.log | head -3 || true
