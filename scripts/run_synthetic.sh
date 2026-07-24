#!/usr/bin/env bash
cd /home/usv/finblade-cctv
rm -f evidence/frames/*.jpg evidence/events.jsonl evidence/alerts.jsonl
.venv/bin/python services/inference/run_cpu.py \
  --config config/cameras.synthetic.yaml --seconds 40 --no-serve > scripts/run_syn.log 2>&1
echo "DONE_RC=$?"
echo "=== metrics.json ==="
cat evidence/metrics.json
echo
echo "=== alerts by rule ==="
if [ -s evidence/alerts.jsonl ]; then
  grep -oE '"rule_id": "[^"]+"' evidence/alerts.jsonl | sort | uniq -c
else
  echo "(no alerts fired)"
fi
echo "alert_lines=$(wc -l < evidence/alerts.jsonl)"
echo "=== last console lines ==="
tail -4 scripts/run_syn.log
