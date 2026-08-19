#!/usr/bin/env bash
cd /home/usv/finblade-cctv
exec .venv/bin/python services/inference/run_cpu.py \
  --config config/cameras.synthetic.yaml --seconds 35 --no-serve \
  --api-url http://127.0.0.1:8000 > scripts/run_live.log 2>&1
