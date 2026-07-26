#!/usr/bin/env bash
cd /home/usv/finblade-cctv
pkill -f 'rtsp_stream.py' 2>/dev/null; sleep 2
nohup .venv/bin/python scripts/rtsp_stream.py media/CAM01_T03_zone_assignment.mp4 rtsp://127.0.0.1:8554/cam01 > scripts/rtsp.log 2>&1 &
echo "=== camera state over ~22s (crosses a ~15s loop boundary) ==="
.venv/bin/python - <<'PY'
import json,urllib.request,time
from collections import Counter
seen=Counter()
for _ in range(11):
    try:
        c=json.loads(urllib.request.urlopen("http://127.0.0.1:8000/api/v1/cameras",timeout=4).read())["cameras"]
        st=next((x["effective_state"] for x in c if x["camera_id"]=="CAM-SYN-01"),"?")
    except Exception: st="err"
    seen[st]+=1; print(f"  t+{_*2:2d}s: {st}")
    time.sleep(2)
print("  summary:",dict(seen))
PY
echo "=== publisher log (expect NO ArgumentError/retry) ==="; grep -icE 'argumenterror|retry' scripts/rtsp.log; tail -2 scripts/rtsp.log
rm -f scripts/_loop.sh
