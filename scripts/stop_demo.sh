#!/usr/bin/env bash
# Stop everything scripts/start_demo.sh started.
cd "$(dirname "$0")/.." || exit 1
pkill -f 'uvicorn services.api.app' && echo "stopped api" || echo "api not running"
pkill -f 'run_cpu.py'               && echo "stopped camera worker(s)" || echo "no camera worker"
sleep 1
pgrep -af 'uvicorn services.api.app|run_cpu.py' && echo "WARNING: still running" \
  || echo "all stopped"
