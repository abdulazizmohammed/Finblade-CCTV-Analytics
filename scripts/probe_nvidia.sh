#!/usr/bin/env bash
echo "=== nvidia-smi ==="
if command -v nvidia-smi >/dev/null; then nvidia-smi; else echo "  nvidia-smi NOT found"; fi
echo "=== torch cuda (current venv) ==="
.venv/bin/python - <<'PY'
import torch
print("torch", torch.__version__)
print("cuda_available", torch.cuda.is_available())
try:
    print("device_count", torch.cuda.device_count())
    if torch.cuda.is_available():
        print("device0", torch.cuda.get_device_name(0))
except Exception as e:
    print("cuda query error:", e)
PY
