"""FinBlade CCTV — CPU inference runner (additive; main.py left intact).

Same spine as main.py but:
  * device: CPU + .pt weights (no OpenVINO/Arc — CLAUDE.md rule 4)
  * source can be a local file OR rtsp (config `source`/`rtsp_url`)
  * detections feed the tested finblade core (zones/debounce/metrics/events/rules)
  * writes the full evidence bundle (frames, contact sheet, metrics.json,
    events.jsonl, alerts.jsonl) per CLAUDE.md's evidence protocol.

It NEVER fakes detection. If cv2 / ultralytics / weights are missing it prints a
blocker and exits non-zero — a green run over a fake detector is banned.

Run (from repo root, once deps + weights exist — see BLOCKERS.md):
    python services/inference/run_cpu.py --config config/cameras.dev.yaml
"""

import argparse
import json
import os
import sys
import threading
import time

# --- hard dependency gate: fail loudly, never fake -------------------------
_MISSING = []
try:
    import cv2  # noqa: F401
except Exception:
    _MISSING.append("opencv-python-headless")
try:
    import numpy as np  # noqa: F401
except Exception:
    _MISSING.append("numpy")
try:
    from ultralytics import YOLO  # noqa: F401
except Exception:
    _MISSING.append("ultralytics")

# Make the repo root importable so `finblade` resolves when run from anywhere.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from finblade.config import load_camera_config          # noqa: E402
from finblade.debounce import BoundaryDebouncer          # noqa: E402
from finblade.events import (                            # noqa: E402
    CAMERA_HEARTBEAT, DENSITY_UPDATE, ZONE_ENTRY, ZONE_EXIT, ZONE_TRANSITION,
    new_event,
)
from finblade.geometry import foot_point                 # noqa: E402
from finblade.identity import PersonRefHasher            # noqa: E402
from finblade.metrics import (                           # noqa: E402
    DwellTracker, FlowCounter, ZoneStateAggregator, density_per_sqm, capacity_pct,
    density_status,
)
from finblade.rules import RuleEngine                    # noqa: E402
from finblade.zones import zone_of                       # noqa: E402

try:
    import requests  # already present (ultralytics dep); used for live POSTs
except Exception:
    requests = None

# Live API sink (set by --api-url). When set, zone-states + alerts are POSTed so
# the dashboard shows live data. Failures are swallowed so inference never stalls.
_API = {"base": None}


def _post(path, payload):
    if not _API["base"] or requests is None:
        return
    try:
        requests.post(_API["base"] + path, json=payload, timeout=0.5)
    except Exception:
        pass  # dashboard is best-effort; never block the pipeline on it

# --- theme-matched overlay colours (BGR = hex channels reversed) -----------
# Source of truth: web/finblade-theme.css :root overlay block.
BGR_TRACK      = (194, 189, 24)   # #18bdc2 person box
BGR_FOOT       = (41, 160, 240)   # #f0a029 foot point
BGR_ZONE       = (224, 220, 79)   # #4fdce0 monitored zone edge
BGR_RESTRICTED = (158, 71, 224)   # #e0479e restricted edge (magenta)
BGR_CRITICAL   = (75, 75, 239)    # #ef4b4b live intrusion (red)
BGR_TEXT       = (240, 236, 220)  # #dcecf0

EVIDENCE = os.path.join(_REPO_ROOT, "evidence")
FRAMES_DIR = os.path.join(EVIDENCE, "frames")

_latest_jpeg = {"buf": None}
_lock = threading.Lock()


def _die_if_missing_deps():
    if _MISSING:
        print("[BLOCKER] cannot run detection — missing:", ", ".join(_MISSING),
              file=sys.stderr)
        print("[BLOCKER] install the CPU stack (see BLOCKERS.md B-1) then retry.",
              file=sys.stderr)
        sys.exit(2)


def _draw_dashed_poly(frame, pts, color, thickness=2, dash=14):
    n = len(pts)
    for i in range(n):
        a = pts[i]
        b = pts[(i + 1) % n]
        dist = int(((b[0]-a[0])**2 + (b[1]-a[1])**2) ** 0.5)
        if dist == 0:
            continue
        steps = max(1, dist // dash)
        for s in range(0, steps, 2):
            t0 = s / steps
            t1 = min(1.0, (s + 1) / steps)
            p0 = (int(a[0]+(b[0]-a[0])*t0), int(a[1]+(b[1]-a[1])*t0))
            p1 = (int(a[0]+(b[0]-a[0])*t1), int(a[1]+(b[1]-a[1])*t1))
            cv2.line(frame, p0, p1, color, thickness)


def annotate(frame, zones, tracks, occupancy):
    for z in zones:
        pts = [(int(x), int(y)) for x, y in z.polygon]
        occ = occupancy.get(z.zone_id, 0)
        if z.restricted:
            # magenta dashed; flash red (solid overlay) when occupied = intrusion.
            _draw_dashed_poly(frame, pts, BGR_RESTRICTED, 2)
            if occ > 0:
                cv2.polylines(frame, [np.array(pts, dtype=np.int32)], True, BGR_CRITICAL, 2)
        else:
            cv2.polylines(frame, [np.array(pts, dtype=np.int32)], True, BGR_ZONE, 2)
        dens = density_per_sqm(occ, z.area_sqm)
        label = f"{z.zone_name}: {occ}/{z.capacity_max}  {dens:.2f}/m2"
        px, py = pts[0]
        cv2.rectangle(frame, (px, py - 22), (px + 330, py), (14, 31, 41), -1)
        cv2.putText(frame, label, (px + 4, py - 6),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, BGR_TEXT, 1)

    for (tid, x1, y1, x2, y2) in tracks:
        fx, fy = foot_point(x1, y1, x2, y2)
        cv2.rectangle(frame, (int(x1), int(y1)), (int(x2), int(y2)), BGR_TRACK, 2)
        cv2.circle(frame, (int(fx), int(fy)), 4, BGR_FOOT, -1)
        cv2.putText(frame, f"ID {tid}", (int(x1), int(y1) - 6),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, BGR_TRACK, 1)
    return frame


def build_contact_sheet(frame_paths, out_path, cols=4):
    if not frame_paths:
        return
    imgs = [cv2.imread(p) for p in frame_paths]
    imgs = [im for im in imgs if im is not None]
    if not imgs:
        return
    h, w = imgs[0].shape[:2]
    scale = 320 / w
    thumbs = [cv2.resize(im, (320, int(h * scale))) for im in imgs]
    th = thumbs[0].shape[0]
    rows = (len(thumbs) + cols - 1) // cols
    sheet = np.zeros((rows * th, cols * 320, 3), dtype=np.uint8)
    for i, t in enumerate(thumbs):
        r, c = divmod(i, cols)
        sheet[r*th:(r+1)*th, c*320:(c+1)*320] = t
    cv2.imwrite(out_path, sheet)


def run(config_path, max_seconds=None):
    _die_if_missing_deps()
    os.makedirs(FRAMES_DIR, exist_ok=True)
    cfg = load_camera_config(config_path)

    if cfg.device.upper() != "CPU":
        print(f"[warn] config device={cfg.device}; forcing CPU per project rule 4.",
              file=sys.stderr)
    device = "cpu"

    if not os.path.exists(cfg.model_path):
        print(f"[BLOCKER] weights not found: {cfg.model_path} (see BLOCKERS.md B-1)",
              file=sys.stderr)
        sys.exit(2)

    model = YOLO(cfg.model_path, task="detect")

    src = cfg.source
    if src and not src.startswith("rtsp") and not os.path.isabs(src):
        src = os.path.join(_REPO_ROOT, src)
    cap = cv2.VideoCapture(src)
    if not cap.isOpened():
        print(f"[BLOCKER] cannot open source: {src}", file=sys.stderr)
        sys.exit(2)

    deb = BoundaryDebouncer(n=3)
    dwell = DwellTracker()
    flow = FlowCounter()
    agg = ZoneStateAggregator(period_s=5.0)
    eng = RuleEngine()
    hasher = PersonRefHasher()

    events_fp = open(os.path.join(EVIDENCE, "events.jsonl"), "w")
    alerts_fp = open(os.path.join(EVIDENCE, "alerts.jsonl"), "w")
    frame_paths = []
    det_counts = []
    seen_ids = set()
    saved_frames = 0
    processed = 0
    t_start = time.time()
    last_still = False
    last_evidence_t = 0.0
    prev_zone = {}

    interval = 1.0 / cfg.process_fps if cfg.process_fps and cfg.process_fps > 0 else 0
    last_proc = 0.0

    while True:
        ok, frame = cap.read()
        if not ok:
            # loop the file for a deterministic demo; reconnect for rtsp.
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = cap.read()
            if not ok:
                break

        if not last_still:
            cv2.imwrite(os.path.join(_REPO_ROOT, "media", "cam1_frame.jpg"), frame)
            last_still = True

        now = time.time()
        if interval and (now - last_proc) < interval:
            continue
        last_proc = now
        vnow = now - t_start

        res = model.track(frame, persist=True, classes=[cfg.person_class_id],
                          conf=cfg.conf_threshold, imgsz=cfg.imgsz,
                          tracker="bytetrack.yaml",
                          device=device, verbose=False)[0]

        tracks = []
        occupancy = {z.zone_id: 0 for z in cfg.zones}
        if res.boxes is not None and res.boxes.id is not None:
            xyxy = res.boxes.xyxy.cpu().numpy()
            ids = res.boxes.id.cpu().numpy().astype(int)
            for (x1, y1, x2, y2), tid in zip(xyxy, ids):
                tid = int(tid)
                seen_ids.add(tid)
                tracks.append((tid, x1, y1, x2, y2))
                observed = zone_of(foot_point(x1, y1, x2, y2), cfg.zones)
                confirmed, changed = deb.update(tid, observed)
                if confirmed:
                    occupancy[confirmed] += 1
                pr = hasher.ref(tid)
                if changed:
                    old = prev_zone.get(tid)
                    if old and confirmed:
                        e = new_event(ZONE_TRANSITION, cfg.camera_id, cfg.site_id, vnow,
                                      zone_from=old, zone_to=confirmed, person_ref=pr)
                    elif confirmed:
                        e = new_event(ZONE_ENTRY, cfg.camera_id, cfg.site_id, vnow,
                                      zone_to=confirmed, person_ref=pr, confidence=0.9)
                        flow.record_entry(confirmed, vnow)
                    else:
                        e = new_event(ZONE_EXIT, cfg.camera_id, cfg.site_id, vnow,
                                      zone_from=old or "NONE", person_ref=pr)
                        if old:
                            flow.record_exit(old, vnow)
                    events_fp.write(json.dumps(e) + "\n")
                    prev_zone[tid] = confirmed
                d = dwell.update(tid, confirmed, vnow)
                zobj = next((z for z in cfg.zones if z.zone_id == confirmed), None)
                if zobj:
                    intr = eng.evaluate_intrusion(pr, confirmed, zobj.restricted, vnow)
                    if intr:
                        alerts_fp.write(json.dumps(intr.as_dict()) + "\n")
                        _post("/api/v1/alerts", intr.as_dict())
                    lo = eng.evaluate_loiter(pr, confirmed, d, vnow)
                    if lo:
                        alerts_fp.write(json.dumps(lo.as_dict()) + "\n")
                        _post("/api/v1/alerts", lo.as_dict())

        det_counts.append(len(tracks))
        processed += 1

        # heartbeat + density events / rules on 5s cadence
        eng.camera.heartbeat(cfg.camera_id, vnow)
        events_fp.write(json.dumps(
            new_event(CAMERA_HEARTBEAT, cfg.camera_id, cfg.site_id, vnow)) + "\n")
        if agg.due(vnow):
            for z in cfg.zones:
                occ = occupancy[z.zone_id]
                dens = density_per_sqm(occ, z.area_sqm)
                cap_pct = capacity_pct(occ, z.capacity_max)
                events_fp.write(json.dumps(
                    new_event(DENSITY_UPDATE, cfg.camera_id, cfg.site_id, vnow,
                              zone_id=z.zone_id, occupancy=occ, density=dens)) + "\n")
                # Live zone-state for the dashboard cards (UC-29).
                _post("/api/v1/zones/state", {
                    "zone_id": z.zone_id, "camera_id": cfg.camera_id,
                    "occupancy": occ, "density": dens, "capacity_pct": cap_pct,
                    "inflow_per_min": flow.inflow_per_min(z.zone_id, vnow),
                    "outflow_per_min": flow.outflow_per_min(z.zone_id, vnow),
                    "status": density_status(dens), "ts": vnow,
                })
                for al in eng.evaluate_zone(z.zone_id, dens, cap_pct, vnow):
                    alerts_fp.write(json.dumps(al.as_dict()) + "\n")
                    _post("/api/v1/alerts", al.as_dict())

        summary = "  ".join(f"{z.zone_name}={occupancy[z.zone_id]}" for z in cfg.zones)
        print(f"[{time.strftime('%H:%M:%S')}] tracked={len(tracks)}  {summary}", flush=True)

        annotated = annotate(frame.copy(), cfg.zones, tracks, occupancy)
        okj, buf = cv2.imencode(".jpg", annotated)
        if okj:
            with _lock:
                _latest_jpeg["buf"] = buf.tobytes()

        # save an evidence frame ~ every 5s of video
        if vnow - last_evidence_t >= 5.0:
            last_evidence_t = vnow
            saved_frames += 1
            p = os.path.join(FRAMES_DIR, f"frame_{saved_frames:04d}.jpg")
            cv2.imwrite(p, annotated)
            frame_paths.append(p)

        if max_seconds and vnow >= max_seconds:
            break

    # finalise evidence
    events_fp.close()
    alerts_fp.close()
    build_contact_sheet(frame_paths, os.path.join(EVIDENCE, "contact_sheet.jpg"))
    elapsed = time.time() - t_start
    metrics = {
        "config": os.path.basename(config_path),
        "device": device,
        "imgsz": cfg.imgsz,
        "conf_threshold": cfg.conf_threshold,
        "frames_processed": processed,
        "avg_fps": round(processed / elapsed, 2) if elapsed else 0.0,
        "detections_per_frame": {
            "avg": round(sum(det_counts) / len(det_counts), 2) if det_counts else 0.0,
            "min": min(det_counts) if det_counts else 0,
            "max": max(det_counts) if det_counts else 0,
        },
        "unique_track_ids": len(seen_ids),
        "evidence_frames_saved": saved_frames,
    }
    with open(os.path.join(EVIDENCE, "metrics.json"), "w") as f:
        json.dump(metrics, f, indent=2)
    print("[info] evidence written to ./evidence/", flush=True)


# --- MJPEG viewer (kept minimal; the dashboard embeds this stream) ---------
def _serve():
    from flask import Flask, Response
    app = Flask(__name__)

    @app.route("/")
    def index():
        return '<img src="/stream" style="max-width:100%">'

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

    app.run(host="0.0.0.0", port=8080, threaded=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/cameras.dev.yaml")
    ap.add_argument("--seconds", type=float, default=None,
                    help="stop after N seconds of video (evidence run)")
    ap.add_argument("--no-serve", action="store_true")
    ap.add_argument("--api-url", default=None,
                    help="POST live zone-states + alerts to this API base "
                         "(e.g. http://127.0.0.1:8000) for the dashboard")
    args = ap.parse_args()

    if args.api_url:
        _API["base"] = args.api_url.rstrip("/")
        print(f"[info] live-posting to {_API['base']}", flush=True)

    if not args.no_serve:
        threading.Thread(target=_serve, daemon=True).start()
    run(args.config, max_seconds=args.seconds)
