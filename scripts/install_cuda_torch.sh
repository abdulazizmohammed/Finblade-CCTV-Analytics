#!/usr/bin/env bash
set -e
cd /home/usv/finblade-cctv
exec > scripts/install_cuda.log 2>&1
PIP=.venv/bin/pip
echo "=== install CUDA torch+torchvision (cu128 for Blackwell) ==="
$PIP install --no-input --force-reinstall torch torchvision \
  --index-url https://download.pytorch.org/whl/cu128
echo "=== restore pinned numpy 1.26.4 (torchvision may pull numpy 2.x) ==="
$PIP install --no-input --no-deps --force-reinstall "numpy==1.26.4"
echo "=== versions ==="
.venv/bin/python - <<'PY'
import torch, torchvision, numpy
print("torch", torch.__version__)
print("torchvision", torchvision.__version__)
print("numpy", numpy.__version__)
print("cuda_available", torch.cuda.is_available())
PY
echo "=== DONE ==="
