#!/usr/bin/env bash
# Get an ffmpeg binary without root.
#
# mediamtx publishes a file by shelling out to ffmpeg, and apt needs a password
# nobody is here to type. imageio-ffmpeg ships a STATIC ffmpeg inside a wheel,
# which is a plain pip install and needs no system packages.
#
# Installed with --target into .pgtest-style tooling dir, not the app venv:
# this is a dev convenience for replaying a clip, not a runtime dependency.
set -u
cd "$(dirname "$0")/.."
mkdir -p scripts/logs
exec > >(tee -a scripts/logs/ffmpeg_install.log) 2>&1
echo "=== $(date -Is)"

TOOLS="$(pwd)/.tools"
mkdir -p "${TOOLS}"

if [ -x "${TOOLS}/ffmpeg" ]; then
  echo "already have ${TOOLS}/ffmpeg"
  "${TOOLS}/ffmpeg" -version 2>&1 | head -1
  exit 0
fi

.venv/bin/pip install --only-binary=:all: --target "${TOOLS}/lib" imageio-ffmpeg 2>&1 | tail -2

PYTHONPATH="${TOOLS}/lib" .venv/bin/python - "${TOOLS}" <<'PY'
import os, shutil, sys
try:
    import imageio_ffmpeg
except ImportError as exc:
    print(f"  imageio-ffmpeg unavailable: {exc}")
    raise SystemExit(3)
src = imageio_ffmpeg.get_ffmpeg_exe()
dst = os.path.join(sys.argv[1], "ffmpeg")
shutil.copy(src, dst)
os.chmod(dst, 0o755)
print(f"  ffmpeg -> {dst}")
PY

"${TOOLS}/ffmpeg" -version 2>&1 | head -1
