#!/usr/bin/env bash
cd /home/usv/finblade-cctv
# kill only the stale CAM01_T02 publisher; keep the user's CAM01_T03 publisher
pkill -f 'CAM01_T02_tracking_occupancy' 2>/dev/null; sleep 4
echo "=== publishers now on cam01 ==="
ps -eo pid,cmd | grep rtsp_stream.py | grep -v grep || echo "  none"
echo "=== camera state (expect ONLINE, stable) ==="
.venv/bin/python - <<'PY'
import json,urllib.request,time
for _ in range(3):
    c=json.loads(urllib.request.urlopen("http://127.0.0.1:8000/api/v1/cameras",timeout=5).read())["cameras"]
    for x in c:
        if x["camera_id"]=="CAM-SYN-01":
            print("  ",x["effective_state"],"fps=",x.get("input_fps"),"res=",x.get("resolution"))
    time.sleep(2)
PY
echo "=== detection ==="; grep -E 'tracked=' scripts/cam01.log | tail -2
echo "=== which clip is mediamtx serving ==="; grep -iE 'publishing to path' scripts/mediamtx.log | tail -2
rm -f scripts/_dedupe.sh
