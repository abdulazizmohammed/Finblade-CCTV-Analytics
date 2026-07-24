"""
FinBlade CCTV — inference spine (single camera).

Pipeline:
    RTSP frame
      -> YOLO (OpenVINO, Arc GPU), person class only
      -> built-in ByteTrack (persistent track IDs)
      -> foot point (bottom-center of bbox) tested against each zone polygon
      -> live occupancy + density per zone
      -> annotated frame served as MJPEG + occupancy printed to console

This proves the whole spine on a deterministic recorded loop before we build
rules/dashboard on top. No supervision dependency yet — uses Ultralytics'
built-in tracker and OpenCV point-in-polygon to keep the first run robust.

NOTE: This is written to run on the Arc box. It was not executed in the
authoring sandbox (no GPU / no network there), so treat the first run as a
shake-out. The version-sensitive spot is the OpenVINO device string ('GPU').
"""

import sys
import time
import threading

import cv2
import yaml
import numpy as np
from flask import Flask, Response
from ultralytics import YOLO

CONFIG_PATH = "/config/cameras.yaml"

# ---------------------------------------------------------------- config
with open(CONFIG_PATH, "r") as f:
    CFG = yaml.safe_load(f)

ZONES = CFG["zones"]
for z in ZONES:
    z["_poly"] = np.array(z["polygon"], dtype=np.int32)

# ---------------------------------------------------------------- model
# Prefer the exported OpenVINO IR dir; fall back to .pt if you haven't exported yet.
model = YOLO(CFG["model_path"], task="detect")

# shared latest annotated frame for the MJPEG endpoint
_latest_jpeg = {"buf": None}
_lock = threading.Lock()


def foot_point(x1, y1, x2, y2):
    """Ground-contact point: bottom-center of the box. We assign zones on the
    feet, not the centroid, so people near a boundary don't flip zones."""
    return (int((x1 + x2) / 2), int(y2))


def zone_of(point):
    """Return zone_id whose polygon contains the point, else None.
    Restricted zones are tested first so an intrusion always wins."""
    for z in sorted(ZONES, key=lambda z: not z["restricted"]):
        if cv2.pointPolygonTest(z["_poly"], point, False) >= 0:
            return z["zone_id"]
    return None


def annotate(frame, tracks, occupancy):
    # draw zone polygons + occupancy/density labels
    for z in ZONES:
        color = (0, 0, 255) if z["restricted"] else (0, 200, 0)
        cv2.polylines(frame, [z["_poly"]], True, color, 2)
        occ = occupancy.get(z["zone_id"], 0)
        density = occ / z["area_sqm"] if z["area_sqm"] else 0.0
        label = f'{z["zone_name"]}: {occ}/{z["capacity_max"]}  {density:.2f}/m2'
        px, py = z["_poly"][0]
        cv2.rectangle(frame, (px, py - 22), (px + 320, py), color, -1)
        cv2.putText(frame, label, (px + 4, py - 6),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)

    # draw tracked people
    for t in tracks:
        x1, y1, x2, y2, tid = t
        fx, fy = foot_point(x1, y1, x2, y2)
        cv2.rectangle(frame, (x1, y1), (x2, y2), (255, 180, 0), 2)
        cv2.circle(frame, (fx, fy), 4, (0, 255, 255), -1)  # foot point
        cv2.putText(frame, f"ID {tid}", (x1, y1 - 6),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 180, 0), 1)
    return frame


def run():
    cap = cv2.VideoCapture(CFG["rtsp_url"], cv2.CAP_FFMPEG)
    if not cap.isOpened():
        print(f"[FATAL] cannot open {CFG['rtsp_url']}", flush=True)
        sys.exit(1)

    target_fps = CFG.get("process_fps", 12)
    frame_interval = 1.0 / target_fps if target_fps and target_fps > 0 else 0
    saved_still = False
    last = 0.0

    while True:
        ok, frame = cap.read()
        if not ok:
            print("[warn] frame read failed, retrying...", flush=True)
            time.sleep(0.5)
            cap.open(CFG["rtsp_url"], cv2.CAP_FFMPEG)
            continue

        # save a reference still once, so you can read off zone polygon coords
        if not saved_still:
            cv2.imwrite("/media/cam1_frame.jpg", frame)
            saved_still = True
            print("[info] saved reference frame -> media/cam1_frame.jpg", flush=True)

        # frame-skip to hold latency down
        now = time.time()
        if frame_interval and (now - last) < frame_interval:
            continue
        last = now

        # detect + track in one call (built-in ByteTrack, persistent IDs)
        results = model.track(
            frame,
            persist=True,
            classes=[CFG["person_class_id"]],
            conf=CFG["conf_threshold"],
            tracker="bytetrack.yaml",
            device=CFG["device"],
            verbose=False,
        )[0]

        tracks = []
        occupancy = {z["zone_id"]: 0 for z in ZONES}

        if results.boxes is not None and results.boxes.id is not None:
            xyxy = results.boxes.xyxy.cpu().numpy().astype(int)
            ids = results.boxes.id.cpu().numpy().astype(int)
            for (x1, y1, x2, y2), tid in zip(xyxy, ids):
                tracks.append((x1, y1, x2, y2, int(tid)))
                zid = zone_of(foot_point(x1, y1, x2, y2))
                if zid:
                    occupancy[zid] += 1

        # console line — your first proof it works
        summary = "  ".join(
            f'{z["zone_name"]}={occupancy[z["zone_id"]]}' for z in ZONES
        )
        print(f"[{time.strftime('%H:%M:%S')}] tracked={len(tracks)}  {summary}",
              flush=True)

        # publish annotated frame for the browser
        annotated = annotate(frame, tracks, occupancy)
        ok, buf = cv2.imencode(".jpg", annotated)
        if ok:
            with _lock:
                _latest_jpeg["buf"] = buf.tobytes()


# ---------------------------------------------------------------- MJPEG server
app = Flask(__name__)


@app.route("/")
def index():
    return '<h3>FinBlade CCTV — spine</h3><img src="/stream" style="max-width:100%">'


@app.route("/stream")
def stream():
    def gen():
        while True:
            with _lock:
                buf = _latest_jpeg["buf"]
            if buf is not None:
                yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + buf + b"\r\n")
            time.sleep(0.05)
    return Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame")


if __name__ == "__main__":
    threading.Thread(target=run, daemon=True).start()
    app.run(host="0.0.0.0", port=8080, threaded=True)
