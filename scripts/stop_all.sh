#!/usr/bin/env bash
for p in $(pgrep -f 'run_cpu.py'); do kill -9 "$p" 2>/dev/null; done
for p in $(pgrep -f 'uvicorn services.api'); do kill -9 "$p" 2>/dev/null; done
sleep 1
left=$(pgrep -af 'run_cpu.py|uvicorn services.api' | grep -v pgrep | wc -l)
echo "remaining: $left"
