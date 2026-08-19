#!/usr/bin/env bash
# Is the demo stack up?
cd "$(dirname "$0")/.." || exit 1
PORT="${PORT:-8000}"
echo "api process    : $(ps -eo args | grep -c '[u]vicorn services.api.app')"
echo "camera process : $(ps -eo args | grep -c '[r]un_cpu.py')"
echo "healthz        : $(curl -s -o /dev/null -m 5 -w '%{http_code}' "http://localhost:$PORT/healthz")"
