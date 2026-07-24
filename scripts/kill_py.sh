#!/usr/bin/env bash
for p in $(pgrep -f 'bench_raw|bench_device|run_cpu.py'); do kill -9 "$p" 2>/dev/null; done
sleep 1
echo "remaining: $(pgrep -af 'bench_raw|bench_device|run_cpu.py' | grep -v pgrep | wc -l)"
