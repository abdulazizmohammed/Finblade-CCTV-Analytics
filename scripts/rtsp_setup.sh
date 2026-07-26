#!/usr/bin/env bash
# One-time setup for RTSP camera simulation: PyAV (publisher) + MediaMTX (server).
# No sudo required. Needs network. Idempotent.
set -e
cd "$(dirname "$0")/.."

echo "[setup] ensuring PyAV (av) is installed in the venv…"
.venv/bin/python -c "import av" 2>/dev/null && echo "  av already installed" || .venv/bin/pip install av

echo "[setup] ensuring MediaMTX binary…"
mkdir -p scripts/bin
if [ -x scripts/bin/mediamtx ]; then
  echo "  already have $(scripts/bin/mediamtx --version 2>/dev/null | head -1)"
else
  URL=$(curl -sL https://api.github.com/repos/bluenviron/mediamtx/releases/latest \
    | grep -oE 'https://[^"]*mediamtx_v[0-9.]+_linux_amd64\.tar\.gz' | head -1)
  [ -n "$URL" ] || { echo "  could not resolve MediaMTX download URL"; exit 1; }
  echo "  downloading $URL"
  curl -sL "$URL" -o /tmp/mtx.tgz
  tar -xzf /tmp/mtx.tgz -C scripts/bin mediamtx
  chmod +x scripts/bin/mediamtx
  echo "  installed $(scripts/bin/mediamtx --version 2>/dev/null | head -1)"
fi
echo "[setup] done. Publish a clip with:  bash scripts/rtsp_stream.sh media/<clip>.mp4 cam01"
