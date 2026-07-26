#!/usr/bin/env bash
cd /home/usv/finblade-cctv
# make sure the RTSP publisher for cam01 is up (mediamtx already running)
pgrep -f 'rtsp_stream.py' >/dev/null || nohup .venv/bin/python scripts/rtsp_stream.py media/CAM01_T03_zone_assignment.mp4 rtsp://127.0.0.1:8554/cam01 > scripts/rtsp.log 2>&1 &
# restart API onto new code (leave other runners/publisher)
pkill -f 'uvicorn services.api' 2>/dev/null; sleep 2
nohup .venv/bin/python -m uvicorn services.api.app:app --host 0.0.0.0 --port 8000 --log-level warning > scripts/api.log 2>&1 &
sleep 4
echo "=== add camera CAM-UI-01 with an RTSP source (like the UI form) ==="
.venv/bin/python - <<'PY'
import json,urllib.request
r=urllib.request.Request("http://127.0.0.1:8000/api/v1/cameras",
  data=json.dumps({"camera_id":"CAM-UI-01","name":"UI RTSP cam","source":"rtsp://127.0.0.1:8554/cam01"}).encode(),
  method="POST",headers={"Content-Type":"application/json"})
print("  POST ->", json.loads(urllib.request.urlopen(r,timeout=6).read()))
PY
echo "=== wait for pipeline to come online ==="
sleep 15
.venv/bin/python - <<'PY'
import json,urllib.request
c=json.loads(urllib.request.urlopen("http://127.0.0.1:8000/api/v1/cameras",timeout=5).read())["cameras"]
for x in c:
    if x["camera_id"]=="CAM-UI-01":
        print("  CAM-UI-01:",x["effective_state"],"fps=",x.get("input_fps"),"stream_url=",x.get("stream_url"),"source=",x.get("source"))
PY
echo "=== pipeline log tail ==="; tail -3 scripts/cam_CAM-UI-01.log 2>/dev/null
echo "=== stop + delete the UI camera (cleanup) ==="
.venv/bin/python - <<'PY'
import json,urllib.request
for path,method in [("/api/v1/cameras/CAM-UI-01/stop","POST"),("/api/v1/cameras/CAM-UI-01","DELETE")]:
    r=urllib.request.Request("http://127.0.0.1:8000"+path,method=method)
    print("  ",method,path,"->",json.loads(urllib.request.urlopen(r,timeout=6).read()))
PY
sleep 2
echo "=== pipeline process gone? ==="; pgrep -f 'camera-id CAM-UI-01' >/dev/null && echo "  STILL RUNNING" || echo "  stopped (good)"
rm -f scripts/_uicam.sh
