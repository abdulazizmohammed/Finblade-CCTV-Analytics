#!/usr/bin/env bash
# Stop every finblade process: cameras, API, and the RTSP server/publishers.
for p in $(pgrep -f 'run_cpu.py'); do kill -9 "$p" 2>/dev/null; done
for p in $(pgrep -f 'uvicorn services.api'); do kill -9 "$p" 2>/dev/null; done
# RTSP path (scripts/rtsp_dual.sh). Without these the publishers linger and hold
# the cam1/cam2 paths, so the next publish fails.
for p in $(pgrep -f 'scripts/rtsp_stream.py'); do kill -9 "$p" 2>/dev/null; done
for p in $(pgrep -f 'scripts/bin/mediamtx'); do kill -9 "$p" 2>/dev/null; done
sleep 1
left=$(pgrep -af 'run_cpu.py|uvicorn services.api|scripts/rtsp_stream.py|scripts/bin/mediamtx' \
       | grep -v pgrep | wc -l)
echo "remaining: $left"
