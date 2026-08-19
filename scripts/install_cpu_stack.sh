#!/usr/bin/env bash
set -e
cd /home/usv/finblade-cctv
exec > scripts/install.log 2>&1   # script writes its own log (POSIX redirect)
PIP=.venv/bin/pip
echo "=== [1/3] torch (CPU-only wheel, no CUDA) ==="
$PIP install --no-input torch --index-url https://download.pytorch.org/whl/cpu
echo "=== [2/3] pinned vision + web stack (matches requirements.txt) ==="
$PIP install --no-input \
  "ultralytics==8.3.40" \
  "opencv-python-headless==4.10.0.84" \
  "numpy==1.26.4" \
  "pyyaml==6.0.2" \
  "flask==3.0.3"
echo "=== [3/3] verify imports ==="
.venv/bin/python - <<'PY'
import torch, cv2, numpy, yaml, flask
import ultralytics
print("torch", torch.__version__, "cuda?", torch.cuda.is_available())
print("cv2", cv2.__version__)
print("numpy", numpy.__version__)
print("ultralytics", ultralytics.__version__)
PY
echo "=== INSTALL DONE ==="
