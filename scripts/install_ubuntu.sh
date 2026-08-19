#!/usr/bin/env bash
#
# Prerequisites + install for a CLEAN Ubuntu server. Idempotent: safe to re-run.
#
#   bash scripts/install_ubuntu.sh              # auto-detect GPU vs CPU
#   FORCE_CPU=1 bash scripts/install_ubuntu.sh  # CPU wheels even if a GPU exists
#   SKIP_APT=1  bash scripts/install_ubuntu.sh  # no sudo available
#
# Follows docs/DEPLOY.md. Every pinned version comes from requirements.txt and
# constraints.txt unchanged — this script never picks a version of its own.
set -euo pipefail
cd "$(dirname "$0")/.." || exit 1

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
die() { printf '\n[BLOCKER] %s\n' "$*" >&2; exit 1; }

# --------------------------------------------------------------- 1. system ---
if [ "${SKIP_APT:-0}" != "1" ]; then
  say "1/6  system packages"
  SUDO=""
  [ "$(id -u)" -ne 0 ] && SUDO="sudo"
  $SUDO apt-get update -qq

  # libglib2.0-0 was renamed libglib2.0-0t64 in Ubuntu 24.04. Ask apt which
  # exists rather than guessing from the release number.
  GLIB=libglib2.0-0
  apt-cache show libglib2.0-0t64 >/dev/null 2>&1 && GLIB=libglib2.0-0t64

  # python3.10 is only in the deadsnakes PPA on 24.04+; on 22.04 it is default.
  PYPKG="python3.10-venv"
  apt-cache show python3.10-venv >/dev/null 2>&1 || PYPKG="python3-venv"

  $SUDO apt-get install -y -qq \
    "$PYPKG" python3-pip \
    libgl1 "$GLIB" \
    git curl ca-certificates
  echo "installed: $PYPKG, libgl1, $GLIB, git, curl"
else
  say "1/6  system packages — SKIPPED (SKIP_APT=1)"
fi

# libGL is the single most likely bring-up failure: ultralytics pulls in the
# FULL opencv-python, which wins the import over opencv-python-headless and
# links against libGL.so.1 — absent on a headless server. Fail here with a
# clear message rather than 10 minutes later inside a camera worker.
if ! ldconfig -p 2>/dev/null | grep -q 'libGL\.so\.1'; then
  echo "WARNING: libGL.so.1 not found. If imports fail later:"
  echo "         sudo apt-get install -y libgl1 libglib2.0-0"
fi

# ------------------------------------------------------------- 2. python -----
say "2/6  python"
PY=""
for candidate in python3.10 python3; do
  command -v "$candidate" >/dev/null 2>&1 && { PY="$candidate"; break; }
done
[ -n "$PY" ] || die "no python3 found"
PYVER="$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
echo "using $PY ($PYVER)"
[ "$PYVER" = "3.10" ] || cat <<EOF
NOTE: 3.10 is the verified version (docs/DEPLOY.md §2). $PYVER may work, but if
      torch or ultralytics fails to resolve a wheel, install python3.10 and
      re-run:  sudo add-apt-repository ppa:deadsnakes/ppa && \\
               sudo apt install python3.10 python3.10-venv
EOF

[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/python -m pip install --quiet --upgrade pip

# ------------------------------------------------------- 3. gpu or cpu -------
say "3/6  choosing wheels"
USE_GPU=0
if [ "${FORCE_CPU:-0}" != "1" ] && command -v nvidia-smi >/dev/null 2>&1 \
   && nvidia-smi >/dev/null 2>&1; then
  USE_GPU=1
  nvidia-smi --query-gpu=name,driver_version,memory.total \
             --format=csv,noheader || true
fi

if [ "$USE_GPU" = "1" ]; then
  echo "NVIDIA GPU detected — installing the +cu128 wheels"
  REQ=requirements.txt
  CON=constraints.txt
  INDEX=(--extra-index-url https://download.pytorch.org/whl/cu128)
else
  echo "no usable GPU — installing CPU wheels"
  echo "expect ~3-5 FPS per camera instead of ~24 (docs/DEPLOY.md §2)"
  # Strip ONLY the +cu128 local-version suffix. The version itself (2.11.0) is
  # untouched, so this selects the CPU build of the same pinned release rather
  # than a different version — see CLAUDE.md rule 2.
  REQ=$(mktemp /tmp/fb_req_XXXX.txt)
  CON=$(mktemp /tmp/fb_con_XXXX.txt)
  sed 's/+cu128//' requirements.txt > "$REQ"
  sed 's/+cu128//' constraints.txt  > "$CON"
  INDEX=(--extra-index-url https://download.pytorch.org/whl/cpu)
fi

# ------------------------------------------------------------ 4. install -----
say "4/6  python packages (this is the slow part — torch is ~2 GB)"
# -c is NOT optional: without it pip resolves boxmot 22, which wants numpy>=2,
# an ABI break for torch/ultralytics/opencv built against numpy 1.x. It breaks
# at RUNTIME, not install time. See the header of constraints.txt.
.venv/bin/pip install -r "$REQ" -c "$CON" "${INDEX[@]}"

# --------------------------------------------------------------- 5. weights --
say "5/6  model weights"
if [ -f models/yolov8n.pt ] && [ -f models/osnet_x0_25_msmt17.pt ]; then
  echo "already present, skipping download"
else
  .venv/bin/python scripts/get_weights.py || cat <<'EOF'
WARNING: weight download failed (no internet?).
         Copy models/yolov8n.pt and models/osnet_x0_25_msmt17.pt from a machine
         that has them. Without OSNet the system still runs — detection,
         tracking, zones, metrics and rules are unaffected — but cross-camera
         identity disables itself and says so in the camera log.
EOF
fi

# ---------------------------------------------------------------- 6. verify --
say "6/6  verify"
.venv/bin/python -c "
import numpy, torch, ultralytics, cv2, fastapi, uvicorn, websockets, flask
print('numpy       ', numpy.__version__, '  <- must be 1.26.4')
print('torch       ', torch.__version__, '| cuda', torch.cuda.is_available())
print('ultralytics ', ultralytics.__version__)
print('opencv      ', cv2.__version__)
print('fastapi     ', fastapi.__version__)
try:
    import boxmot; print('boxmot      ', boxmot.__version__)
except Exception as exc:
    print('boxmot       MISSING —', exc, '(cross-camera identity will disable itself)')
" </dev/null

echo
.venv/bin/python -m unittest discover -s tests 2>&1 | tail -3

cat <<'EOF'

Done. Next:

  bash scripts/start_demo.sh      # API + one camera on a local clip
  bash scripts/verify_demo.sh     # prove it is up
  bash scripts/status_demo.sh     # is it still up

  Dashboard   http://<host>:8000/web/dashboard.html
  Zone editor http://<host>:8000/tools/zone-editor.html

Set FINBLADE_API_KEY (operator) and FINBLADE_INTEGRATION_KEY (the scoped key an
integration gets) before exposing this to anything. Unset, the API is open.
EOF
