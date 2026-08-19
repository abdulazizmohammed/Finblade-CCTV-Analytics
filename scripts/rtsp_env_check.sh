#!/usr/bin/env bash
# What is available to publish a file over RTSP?
set -u
cd "$(dirname "$0")/.."

echo "== tools"
for b in ffmpeg ffprobe; do
  p=$(command -v "$b" 2>/dev/null)
  echo "  $b: ${p:-MISSING}"
done
if [ -x scripts/bin/mediamtx ]; then
  echo "  mediamtx: scripts/bin/mediamtx"
  scripts/bin/mediamtx --version 2>&1 | head -1 | sed 's/^/    /'
else
  echo "  mediamtx: MISSING"
fi

echo
echo "== mediamtx config"
if [ -f config/mediamtx.yml ]; then
  grep -vE '^\s*#|^\s*$' config/mediamtx.yml | head -20 | sed 's/^/  /'
else
  echo "  config/mediamtx.yml absent"
fi

echo
echo "== the clip"
CLIP="media/1903279-uhd_1920_1440_30fps.mp4"
if [ -f "${CLIP}" ]; then
  ls -la "${CLIP}" | sed 's/^/  /'
else
  echo "  ${CLIP} NOT FOUND"
  ls media/*.mp4 2>/dev/null | sed 's/^/  have: /'
fi

echo
echo "== ports"
ss -ltn 2>/dev/null | grep -E ':(8554|8000)\b' || echo "  nothing on 8554; API expected on 8000"
