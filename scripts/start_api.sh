#!/usr/bin/env bash
cd /home/usv/finblade-cctv
exec .venv/bin/python -m uvicorn services.api.app:app \
  --host 0.0.0.0 --port 8000 --log-level info > scripts/api.log 2>&1
