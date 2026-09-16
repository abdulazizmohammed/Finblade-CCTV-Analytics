#!/usr/bin/env bash
# Start the MCP server beside the API. Reads .env for the keys, like start_stack.sh.
#   bash scripts/start_mcp.sh            # http://0.0.0.0:8010/mcp
#   FINBLADE_MCP_PORT=8011 bash scripts/start_mcp.sh
set -euo pipefail
cd "$(dirname "$0")/.."
if [ -f .env ]; then set -a; . ./.env; set +a; fi
: "${FINBLADE_MCP_PORT:=8010}"
# The MCP server talks to the API with the INTEGRATION key when one is set.
export CCTV_API_KEY="${CCTV_API_KEY:-${FINBLADE_INTEGRATION_KEY:-}}"
if [ -z "${FINBLADE_MCP_TOKEN:-}" ]; then
  echo "== FINBLADE_MCP_TOKEN is not set: the MCP endpoint will be OPEN. Set it in .env for anything beyond localhost. =="
fi
pkill -f 'services.mcp.server' 2>/dev/null || true
nohup .venv/bin/python -m services.mcp.server --port "$FINBLADE_MCP_PORT" > scripts/mcp.log 2>&1 &
sleep 1
echo "== MCP on http://0.0.0.0:${FINBLADE_MCP_PORT}/mcp (log: scripts/mcp.log) =="
tail -2 scripts/mcp.log
