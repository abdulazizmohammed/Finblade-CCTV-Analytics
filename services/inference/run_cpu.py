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
import logging
import os
import sys
import threading
import time
from collections import deque

log = logging.getLogger("finblade.inference")

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
    CAMERA_HEARTBEAT, CAMERA_OFFLINE, CAMERA_ONLINE, CAMERA_RECOVERED,
    CAPACITY_WARNING, DENSITY_UPDATE, LOITERING_END, LOITERING_START,
    RESTRICTED_ZONE_ENTRY, RESTRICTED_ZONE_EXIT,
    ZONE_ENTRY, ZONE_EXIT, ZONE_TRANSITION, new_event,
)
from finblade.geometry import foot_point                 # noqa: E402
from finblade.identity import PersonRefHasher            # noqa: E402
from finblade.metrics import (                           # noqa: E402
    DwellTracker, FlowCounter, ZoneStateAggregator, ZoneStats, density_per_sqm,
    capacity_pct, density_status,
)
from finblade.rules import RuleEngine                    # noqa: E402
from finblade.tracking import TrackReaper                # noqa: E402
from finblade.tracks import TrackRegistry                # noqa: E402
from finblade.zones import in_ignored_region, zone_of        # noqa: E402
from services.inference.camera_worker import CameraWorker, CameraState  # noqa: E402
from services.inference.reid_client import ReIDResolver                 # noqa: E402

# Shared handle so the MJPEG server can drive the demo simulate/restore controls.
_worker = {"ref": None}

try:
    import requests  # already present (ultralytics dep); used for live POSTs
except Exception:
    requests = None

# Live API sink (set by --api-url). When set, zone-states + alerts are POSTed so
# the dashboard shows live data. Failures are swallowed so inference never stalls.
_API = {"base": None}
_STREAM = {"url": None}   # this runner's MJPEG stream URL (for the health screen)

# The worker is a first-class API client, so it needs the key like any other.
# Without it, under FINBLADE_API_KEY every call below 401s, and a 401 is NOT an
# exception in requests — so the failure is completely silent. Three things then
# break at once, none of them obviously auth-related:
#   * heartbeats stop landing -> the camera ages into OFFLINE -> the API drops
#     its stream_url -> the dashboard's feed tiles vanish
#   * _fetch_zones_raw reads the 401 body as "no zones" -> occupancy goes to 0
#   * identity resolve stops -> no cross-camera matching
# The key comes from the environment, which the API's camera_manager already
# passes down when it spawns us.
_AUTH_WARNED = {"done": False}


def _auth_headers():
    key = os.environ.get("FINBLADE_API_KEY")
    return {"Authorization": "Bearer %s" % key} if key else {}


# How often the worker publishes live counts, and how much history it smooths
# over. These were effectively 5s and "no smoothing", which produced the visible
# complaint: the dashboard showed 1 person while 2 were standing there.
#
# Two separate faults, and both had to go:
#   * the POST happened every 5s, so any change waited up to 5s to be published
#   * the value sent was the count from the SINGLE frame that coincided with the
#     tick. A person detected in 80% of frames is missed by 1 sample in 5, and
#     that wrong number then sat on screen for the whole interval.
# Publishing a MEDIAN over a short window fixes the second: one bad frame cannot
# move a median, so the number is both responsive and steady.
#
# The 5s cadence for zone-state aggregates, density events and rule evaluation is
# UNCHANGED — that is a specified aggregation window, not a UI refresh rate.
LIVE_POST_INTERVAL = float(os.environ.get("FINBLADE_HEALTH_INTERVAL", "1.0"))
LIVE_WINDOW_SECONDS = float(os.environ.get("FINBLADE_LIVE_WINDOW", "1.5"))


def _median_int(values, fallback=0):
    """Upper median of a small int sequence. Deterministic, and never a .5."""
    if not values:
        return fallback
    s = sorted(values)
    return int(s[len(s) // 2])


def _check_auth(r, path):
    """Say so, once, if the API is rejecting us. Silence here cost a deployment."""
    if r is not None and r.status_code == 401 and not _AUTH_WARNED["done"]:
        _AUTH_WARNED["done"] = True
        print("[BLOCKER] API returned 401 for %s — this worker has no valid API "
              "key. Heartbeats, zones and identity are ALL failing silently. "
              "Set FINBLADE_API_KEY in the worker's environment." % path,
              file=sys.stderr, flush=True)
    return r


def _post(path, payload):
    if not _API["base"] or requests is None:
        return
    try:
        _check_auth(requests.post(_API["base"] + path, json=payload,
                                  headers=_auth_headers(), timeout=0.5), path)
    except Exception:
        pass  # dashboard is best-effort; never block the pipeline on it


def _post_json(path, payload):
    """POST and return the parsed JSON response, or None (best-effort)."""
    if not _API["base"] or requests is None:
        return None
    try:
        r = _check_auth(requests.post(_API["base"] + path, json=payload,
                                      headers=_auth_headers(), timeout=0.5), path)
        if r.status_code == 401:
            return None          # not a result — don't let callers parse the error body
        return r.json()
    except Exception:
        return None


def _fetch_zones_raw(camera_id):
    """Fetch this camera's editor-saved zones (raw dicts).

    Returns a list (possibly EMPTY, meaning 'editor has no zones -> use config'),
    or None when the API is unreachable (meaning 'keep whatever we have')."""
    if requests is None or not _API["base"]:
        return None
    try:
        r = _check_auth(requests.get(_API["base"] + "/api/v1/zones",
                                     params={"camera_id": camera_id},
                                     headers=_auth_headers(), timeout=1.0),
                        "/api/v1/zones")
        # A 401 must read as "unreachable", NOT as "no zones". The error body has
        # no "zones" key, so .get("zones", []) would return [] and silently wipe
        # every zone this camera has — occupancy reading 0 with boxes plainly on
        # people. Keeping what we have is the safe failure.
        if r.status_code == 401:
            return None
        return r.json().get("zones", [])
    except Exception:
        return None


def _zones_from_raw(data, frame_width, frame_height):
    """Convert raw zone dicts to Zone objects (normalized -> pixel at our res, so
    zones scale to this runner's frame size regardless of where they were drawn)."""
    from finblade.zones import zone_from_dict
    out = []
    for z in data:
        if z.get("normalized_polygon"):
            z = dict(z); z.pop("polygon", None)   # force normalized -> pixel scaling
        out.append(zone_from_dict(z, frame_width, frame_height))
    return out


def _zone_sig(data):
    """Change signature so a live edit is detected without rebuilding every tick.
    Includes every editable field (capacity/area too) so any edit hot-reloads."""
    return json.dumps([[z.get("zone_id"), z.get("zone_type"), z.get("restricted"),
                        z.get("normalized_polygon") or z.get("polygon"),
                        z.get("capacity_max"), z.get("area_sqm"),
                        z.get("warning_density"), z.get("critical_density"),
                        z.get("loitering_threshold_sec"), z.get("enabled")]
                       for z in data], sort_keys=True)


def _load_zones_from_api(camera_id, frame_width, frame_height):
    """Startup convenience: raw fetch -> Zone list (or None)."""
    raw = _fetch_zones_raw(camera_id)
    return _zones_from_raw(raw, frame_width, frame_height) if raw else None

# --- theme-matched overlay colours (BGR = hex channels reversed) -----------
# Source of truth: web/finblade-theme.css :root overlay block.
BGR_TRACK      = (194, 189, 24)   # #18bdc2 person box
BGR_FOOT       = (41, 160, 240)   # #f0a029 foot point
BGR_ZONE       = (224, 220, 79)   # #4fdce0 monitored zone edge
BGR_RESTRICTED = (158, 71, 224)   # #e0479e restricted edge (magenta)
BGR_CRITICAL   = (75, 75, 239)    # #ef4b4b live intrusion (red)
BGR_WARNING    = (41, 160, 240)   # #f0a029 loitering highlight (amber)
BGR_TEXT       = (240, 236, 220)  # #dcecf0
BGR_IGNORED    = (128, 128, 128)  # muted grey — detection mask, not a status

EVIDENCE = os.path.join(_REPO_ROOT, "evidence")
FRAMES_DIR = os.path.join(EVIDENCE, "frames")
BOOKMARKS_DIR = os.path.join(EVIDENCE, "bookmarks")   # saved frame per event/alert

# Movement events pushed to the history store.
ZONE_EVENT_TYPES = {ZONE_ENTRY, ZONE_EXIT, ZONE_TRANSITION}
POST_EVENT_TYPES = {ZONE_ENTRY, ZONE_EXIT, ZONE_TRANSITION, DENSITY_UPDATE,
                    CAPACITY_WARNING, RESTRICTED_ZONE_ENTRY, RESTRICTED_ZONE_EXIT,
                    LOITERING_START, LOITERING_END}
# Alerts that get a saved snapshot: critical density (R-02) and restricted-zone
# intrusion (R-06) ONLY.
#
# R-05 (loitering) was dropped from this set deliberately. Loitering fires
# continuously for anyone standing still, so on a looping clip it produced 7,741
# frames totalling 944 MB — the evidence directory became the largest thing in
# the repo and the genuinely serious snapshots were buried in it. A snapshot is
# only worth writing when someone must look at it: a red-band crowd density, or
# a person somewhere they are not allowed to be.
SNAPSHOT_RULES = {"R-02", "R-06"}

_latest_jpeg = {"buf": None}
# Raw frame + render context so the MJPEG stream can re-annotate per-request with
# a viewer's overlay toggles (the pre-encoded buf above is the all-layers default).
_render = {"frame": None, "zones": None, "tracks": None, "occ": None, "meta": None}
_lock = threading.Lock()


def _resolve_device(want):
    """Map config `device` to an Ultralytics device string.

    'cpu' -> cpu. 'cuda'/'gpu'/'nvidia'/'0' -> NVIDIA CUDA if available, else CPU.
    (Intel Arc/OpenVINO is a separate backend handled by main.py, not here.)
    """
    w = str(want).strip().lower()
    if w == "cpu":
        return "cpu"
    if w in ("cuda", "cuda:0", "gpu", "nvidia", "0"):
        try:
            import torch
            if torch.cuda.is_available():
                print(f"[info] using CUDA GPU: {torch.cuda.get_device_name(0)}", flush=True)
                return "cuda:0"
        except Exception:
            pass
        print(f"[warn] device '{want}' requested but CUDA unavailable; using CPU",
              file=sys.stderr)
        return "cpu"
    print(f"[warn] device '{want}' not supported here (cpu/cuda only); using CPU",
          file=sys.stderr)
    return "cpu"


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


# Overlay layers the live stream can toggle on/off (evidence always draws all).
OVERLAY_DEFAULT = {"zones": True, "boxes": True, "ids": True, "feet": True,
                   "dwell": True, "gid": True}


def annotate(frame, zones, tracks, occupancy, track_meta=None, overlay=None):
    track_meta = track_meta or {}
    o = dict(OVERLAY_DEFAULT)
    if overlay:
        o.update(overlay)
    if o["zones"]:
        for z in zones:
            pts = [(int(x), int(y)) for x, y in z.polygon]
            occ = occupancy.get(z.zone_id, 0)
            if getattr(z, "zone_type", "") == "UNMONITORED":
                # Detection mask (mirror, TV, poster, window). Muted grey and
                # dashed: it is not a status and must not compete with the
                # restricted magenta or the critical red for attention — it is
                # simply a region the system has been told to ignore.
                _draw_dashed_poly(frame, pts, BGR_IGNORED, 1)
                px0, py0 = pts[0]
                cv2.putText(frame, f"IGNORED: {z.zone_name}", (px0 + 4, py0 - 6),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.45, BGR_IGNORED, 1)
                continue          # no occupancy/density label — nothing is counted here
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

    restricted_ids = {z.zone_id for z in zones if z.restricted}
    for (tid, x1, y1, x2, y2) in tracks:
        fx, fy = foot_point(x1, y1, x2, y2)
        meta = track_meta.get(tid, {})
        dwell = meta.get("dwell", 0.0)
        loiter = meta.get("loiter", False)
        in_restricted = zone_of((fx, fy), zones) in restricted_ids
        # Priority: restricted intrusion (red) > loitering (amber) > normal (teal).
        if in_restricted:
            box_color, tag = BGR_CRITICAL, "INTRUSION"
        elif loiter:
            box_color, tag = BGR_WARNING, "LOITERING"
        else:
            box_color, tag = BGR_TRACK, ""
        thick = 3 if (in_restricted or loiter) else 2
        if o["boxes"]:
            cv2.rectangle(frame, (int(x1), int(y1)), (int(x2), int(y2)), box_color, thick)
        if o["feet"]:
            cv2.circle(frame, (int(fx), int(fy)), 4, BGR_FOOT, -1)
        if o["ids"] or (tag and o["boxes"]):
            label = f"ID {tid}" if o["ids"] else ""
            if o["ids"] and o["dwell"] and dwell >= 1:
                label += f" {int(dwell)}s"
            if tag:
                label += f"  {tag}"
            cv2.putText(frame, label.strip(), (int(x1), int(y1) - 6),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, box_color, 1)
        if o["gid"]:
            # The CROSS-CAMERA identity, drawn above the local track id.
            # "ID 985" is a ByteTrack counter — private to this process and
            # guaranteed to differ on another camera. The short ref below is
            # the shared one: the SAME person shows the same #xxxx on every
            # camera that recognises them. Without this on screen there is no
            # way to see cross-camera matching working.
            gref = meta.get("gref")
            gid_label = f"#{gref[-4:]}" if gref else "#...."   # .... = not resolved yet
            cv2.putText(frame, gid_label, (int(x1), int(y1) - 24),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                        BGR_TEXT if gref else (120, 120, 120), 2 if gref else 1)
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


def run(config_path, max_seconds=None, source=None, camera_id=None, site_id=None):
    _die_if_missing_deps()
    os.makedirs(FRAMES_DIR, exist_ok=True)
    cfg = load_camera_config(config_path)
    if camera_id:
        cfg.camera_id = camera_id                 # --camera-id (UI-provisioned cams)
    if site_id:
        cfg.site_id = site_id
    if source:
        cfg.source = source                       # --source overrides the YAML clip
        log.info("source overridden to %s", source)
    if str(cfg.source).startswith("rtsp"):
        # RTSP over TCP is far more reliable than the UDP default on LAN/loopback.
        os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

    # Auto-detect the real frame size from the source so normalized editor zones
    # scale correctly to ANY clip, regardless of the config's frame_width/height.
    try:
        import cv2 as _cv2
        _cap = _cv2.VideoCapture(cfg.source)
        _w, _h = int(_cap.get(3)), int(_cap.get(4))
        _cap.release()
        if _w > 0 and _h > 0 and (_w, _h) != (cfg.frame_width, cfg.frame_height):
            log.info("source is %dx%d; overriding config frame size %dx%d",
                     _w, _h, cfg.frame_width, cfg.frame_height)
            cfg.frame_width, cfg.frame_height = _w, _h
    except Exception:
        pass

    device = _resolve_device(cfg.device)

    # Prefer editor-saved zones from the API (source of truth); YAML is the seed
    # we revert to if the editor's zone set is cleared. zone_sig lets the run loop
    # hot-reload zones (including a revert-to-config) when they change in the editor.
    config_zones = list(cfg.zones)
    _raw_zones = _fetch_zones_raw(cfg.camera_id)
    zone_sig = None
    if _raw_zones:
        cfg.zones = _zones_from_raw(_raw_zones, cfg.frame_width, cfg.frame_height)
        zone_sig = _zone_sig(_raw_zones)
        log.info("loaded %d zone(s) from API for %s", len(cfg.zones), cfg.camera_id)
    else:
        if _raw_zones is not None:        # API up but no editor zones -> config seed
            zone_sig = _zone_sig([])
        log.info("using %d zone(s) from config for %s", len(cfg.zones), cfg.camera_id)

    if not os.path.exists(cfg.model_path):
        print(f"[BLOCKER] weights not found: {cfg.model_path} (see BLOCKERS.md B-1)",
              file=sys.stderr)
        sys.exit(2)

    model = YOLO(cfg.model_path, task="detect")

    src = cfg.source
    is_file = bool(src) and "://" not in str(src)
    if is_file and not os.path.isabs(src):
        src = os.path.join(_REPO_ROOT, src)

    # Pace a file source at its native FPS so the capture thread behaves like a
    # real camera (not spinning through the whole clip instantly). RTSP self-paces.
    pace = None
    if is_file:
        probe = cv2.VideoCapture(src)
        opened = probe.isOpened()
        fps = probe.get(cv2.CAP_PROP_FPS) if opened else 0.0
        probe.release()
        if not opened:
            print(f"[BLOCKER] cannot open source: {src}", file=sys.stderr)
            sys.exit(2)
        pace = fps if fps and fps > 0 else 25.0

    worker = CameraWorker(src, cfg.camera_id, loop_file=True,
                          offline_seconds=cfg.offline_seconds, pace_fps=pace)
    _worker["ref"] = worker
    worker.start()

    deb = BoundaryDebouncer(n=3)
    dwell = DwellTracker()
    flow = FlowCounter()
    agg = ZoneStateAggregator(period_s=5.0)
    zstats = ZoneStats()
    eng = RuleEngine()
    hasher = PersonRefHasher()
    # Cross-camera identity. Local ByteTrack ids mean nothing outside this
    # process, so the API resolves them to a shared global_ref. If the weights
    # are missing this disables itself loudly and the rest of the pipeline is
    # unaffected — it never falls back to a stub embedder.
    _reid_cfg = cfg.reid or {}
    reid = ReIDResolver(
        cfg.camera_id, _post_json,
        weights=_reid_cfg.get("weights"),
        device=str(_reid_cfg.get("device", "0")),
        interval_s=float(_reid_cfg.get("interval_seconds", 1.0)),
        max_samples=int(_reid_cfg.get("max_samples", 5)),
        budget_per_frame=int(_reid_cfg.get("budget_per_frame", 8)),
        min_samples_to_resolve=int(_reid_cfg.get("min_samples_to_resolve", 2)),
        enabled=bool(_reid_cfg.get("enabled", True)),
        min_crop_height=float(_reid_cfg.get("min_crop_height", 96.0)),
        min_crop_confidence=float(_reid_cfg.get("min_crop_confidence", 0.5)),
    )
    reid.load()
    reaper = TrackReaper(cfg.track_ttl_seconds)
    registry = TrackRegistry()
    completed_tracks = 0

    os.makedirs(BOOKMARKS_DIR, exist_ok=True)
    slug = cfg.camera_id.replace("/", "_")   # namespace evidence per camera
    events_fp = open(os.path.join(EVIDENCE, f"events_{slug}.jsonl"), "w")
    alerts_fp = open(os.path.join(EVIDENCE, f"alerts_{slug}.jsonl"), "w")
    frame_paths = []
    det_counts = []
    seen_ids = set()
    saved_frames = 0
    bookmark_seq = 0
    processed = 0
    t_start = time.time()
    last_still = False
    last_evidence_t = 0.0
    prev_zone = {}
    restricted_since = {}   # tid -> vnow when the track entered its restricted zone
    feet_total = 0          # foot points evaluated while zones were defined
    feet_outside = 0        # ...that landed in no zone at all
    feet_at_frame_edge = 0  # ...of those, ones sitting on the frame border
    ignored_dets = 0        # detections dropped by an UNMONITORED mask zone
    # Last known people counts, reported in the 5s health post. These must live
    # OUTSIDE the frame loop: the health post runs on its own timer and fires
    # before the first frame is ever processed, so reading the loop's own
    # `tracks` there raised UnboundLocalError and killed the camera process.
    people_in_view = 0
    people_in_zones = 0
    live_window = deque()      # (ts, people_in_view, people_in_zones) per frame
    last_zone_warn = 0.0

    interval = 1.0 / cfg.process_fps if cfg.process_fps and cfg.process_fps > 0 else 0
    last_proc = 0.0
    last_seq = -1
    last_state = None
    last_health_log = 0.0
    last_health_post = 0.0
    last_zone_check = 0.0
    ever_online = False
    restricted_zone_ids = {z.zone_id for z in cfg.zones if z.restricted}
    loiter_zone = {z.zone_id: z.loitering_threshold_sec for z in cfg.zones}
    loiter_started = set()   # (person_ref, zone_id) with a LOITERING_START emitted

    while True:
        now = time.time()
        elapsed = now - t_start
        if max_seconds and elapsed >= max_seconds:
            break

        # Camera state transitions -> health events (independent of detection).
        state = worker.state(now)
        if state != last_state:
            hv = worker.health(now)
            log.info("camera %s state -> %s (%s)", cfg.camera_id, state, hv)
            cev = None
            if state == CameraState.OFFLINE and ever_online:
                cev = new_event(CAMERA_OFFLINE, cfg.camera_id, cfg.site_id, now,
                                last_seen=hv.get("last_valid_ts") or now)
            elif state == CameraState.ONLINE:
                cev = new_event(CAMERA_ONLINE if not ever_online else CAMERA_RECOVERED,
                                cfg.camera_id, cfg.site_id, now)
                if ever_online:
                    # reconnect (incl. a looping test clip restarting): fresh scene,
                    # so drop all rule latches + per-track state -> alerts re-fire.
                    eng.reset_scene()
                    loiter_started.clear(); restricted_since.clear(); prev_zone.clear()
                ever_online = True
            if cev is not None:
                events_fp.write(json.dumps(cev) + "\n")
                _post("/api/v1/events/ingest", cev)
            last_state = state
        if now - last_health_log >= 15.0:
            last_health_log = now
            log.debug("camera %s health %s", cfg.camera_id, worker.health(now))

        # Push a health snapshot to the API every 5s so the camera-health screen
        # reflects live state; the response carries the desired control state,
        # which we apply here (central simulate/restore, no inbound socket needed).
        if now - last_health_post >= LIVE_POST_INTERVAL:
            last_health_post = now
            hv = worker.health(now)
            hv["stream_url"] = _STREAM["url"]
            # People currently in view, counted WITHOUT any zone. Occupancy is
            # a zone measure and reads 0 on a camera with no polygons drawn,
            # which on a mixed site makes those cameras look empty when they
            # are not. This is the raw tracked-person count for the frame, so a
            # site with some zoned and some unzoned cameras still totals
            # correctly. Independent of ReID too — a person counts here the
            # moment they are tracked, without waiting to be identified.
            # Median over the recent window, not this frame's value — see the
            # LIVE_POST_INTERVAL note. Falls back to the instantaneous count only
            # before the window has filled.
            hv["people_in_view"] = _median_int(
                [w[1] for w in live_window], people_in_view)
            hv["people_in_zones"] = _median_int(
                [w[2] for w in live_window], people_in_zones)
            resp = _post_json("/api/v1/cameras/health",
                              {"camera_id": cfg.camera_id, "site_id": cfg.site_id,
                               "ts": now, "health": hv})
            control = (resp or {}).get("control") or {}
            want_sim = bool(control.get("simulate"))
            if want_sim and not worker.simulating:
                worker.simulate_failure()
            elif not want_sim and worker.simulating:
                worker.restore()

        # Hot-reload zones when they change in the editor (no restart needed).
        # raw is None only if the API is unreachable -> keep current. An empty list
        # means the editor's zones were cleared -> revert live to the config seed.
        if now - last_zone_check >= 4.0:
            last_zone_check = now
            raw = _fetch_zones_raw(cfg.camera_id)
            if raw is not None:
                sig = _zone_sig(raw)
                if sig != zone_sig:
                    cfg.zones = (_zones_from_raw(raw, cfg.frame_width, cfg.frame_height)
                                 if raw else list(config_zones))
                    restricted_zone_ids = {z.zone_id for z in cfg.zones if z.restricted}
                    loiter_zone = {z.zone_id: z.loitering_threshold_sec for z in cfg.zones}
                    zone_sig = sig
                    log.info("hot-reloaded %d zone(s) for %s (%s)", len(cfg.zones),
                             cfg.camera_id, "editor" if raw else "reverted to config")

        # Pull the latest frame from the capture thread (non-blocking).
        frame, fts, seq = worker.read_latest()
        if frame is None or seq == last_seq:
            time.sleep(0.01)                 # no fresh frame yet / camera down
            continue
        if interval and (now - last_proc) < interval:
            time.sleep(0.002)                # detection-rate throttle (decoupled)
            continue
        last_proc = now
        last_seq = seq
        vnow = now                           # wall-clock epoch: stamps events/state

        if not last_still:
            try:
                cv2.imwrite(os.path.join(_REPO_ROOT, "media", f"{slug}_frame.jpg"), frame)
            except Exception:
                log.warning("could not save reference still for %s", cfg.camera_id)
            last_still = True

        res = model.track(frame, persist=True, classes=[cfg.person_class_id],
                          conf=cfg.conf_threshold, iou=cfg.iou, imgsz=cfg.imgsz,
                          max_det=cfg.max_det, tracker="bytetrack.yaml",
                          device=device, verbose=False)[0]

        # --- pass 1: detect -> zone -> occupancy; collect events/alerts ------
        tracks = []
        conf_by_tid = {}     # tid -> detection confidence (ReID crop gating)
        zone_by_tid = {}     # tid -> confirmed zone (stamped on the identity)
        occupancy = {z.zone_id: 0 for z in cfg.zones}
        pending_events = []   # ZONE_* + DENSITY_UPDATE dicts
        pending_alerts = []   # alert dicts (intrusion / loiter / density / capacity)
        pending_states = []   # 5s zone-state dicts
        if res.boxes is not None and res.boxes.id is not None:
            xyxy = res.boxes.xyxy.cpu().numpy()
            ids = res.boxes.id.cpu().numpy().astype(int)
            confs = res.boxes.conf.cpu().numpy()
            for (x1, y1, x2, y2), tid, conf in zip(xyxy, ids, confs):
                tid = int(tid)
                _fx, _fy = foot_point(x1, y1, x2, y2)
                # Detection mask. A person in a mirror, on a TV, or on a poster
                # is genuinely person-shaped and the detector is right to fire —
                # so the only reliable filter is a human-drawn polygon over the
                # parts of the frame that are not real floor. Drop the whole
                # detection here, before tracking, dwell, occupancy or ReID:
                # a reflection that is merely uncounted still enters the
                # identity gallery and becomes a phantom others can match on.
                if in_ignored_region((_fx, _fy), cfg.zones):
                    ignored_dets += 1
                    continue
                seen_ids.add(tid)
                reaper.see(tid, vnow)
                tracks.append((tid, x1, y1, x2, y2))
                conf_by_tid[tid] = float(conf)
                observed = zone_of((_fx, _fy), cfg.zones)
                # Diagnostic for the commonest zone-drawing mistake. Occupancy
                # counts FOOT points, and a person whose box is clipped by the
                # frame edge has their foot point ON that edge — so a zone drawn
                # even slightly inset from the bottom excludes them and reports
                # 0 while people are plainly being tracked. Counting this makes
                # a mis-drawn polygon visible instead of silently wrong.
                if cfg.zones:
                    feet_total += 1
                    if observed is None:
                        feet_outside += 1
                        # "Near the bottom" rather than exactly on it: a clipped
                        # person's foot point lands within a few percent of the
                        # border, not on the last pixel, so a 2px test missed
                        # almost all of them and under-reported the cause.
                        if _fy >= (cfg.frame_height * 0.97):
                            feet_at_frame_edge += 1
                confirmed, changed = deb.update(tid, observed)
                zone_by_tid[tid] = confirmed
                if confirmed:
                    occupancy[confirmed] += 1
                pr = hasher.ref(tid)
                if changed:
                    old = prev_zone.get(tid)
                    if old and confirmed:
                        pending_events.append(new_event(
                            ZONE_TRANSITION, cfg.camera_id, cfg.site_id, vnow,
                            zone_from=old, zone_to=confirmed, person_ref=pr))
                    elif confirmed:
                        pending_events.append(new_event(
                            ZONE_ENTRY, cfg.camera_id, cfg.site_id, vnow,
                            zone_to=confirmed, person_ref=pr, confidence=0.9))
                        flow.record_entry(confirmed, vnow)
                    else:
                        pending_events.append(new_event(
                            ZONE_EXIT, cfg.camera_id, cfg.site_id, vnow,
                            zone_from=old or "NONE", person_ref=pr))
                        if old:
                            flow.record_exit(old, vnow)
                    # restricted-zone entry / exit events
                    if confirmed in restricted_zone_ids:
                        restricted_since[tid] = vnow
                        pending_events.append(new_event(
                            RESTRICTED_ZONE_ENTRY, cfg.camera_id, cfg.site_id, vnow,
                            zone_id=confirmed, person_ref=pr))
                    if old in restricted_zone_ids and confirmed not in restricted_zone_ids:
                        pending_events.append(new_event(
                            RESTRICTED_ZONE_EXIT, cfg.camera_id, cfg.site_id, vnow,
                            zone_id=old, person_ref=pr,
                            duration=round(vnow - restricted_since.pop(tid, vnow), 2)))
                        # a "visit" ended -> re-entry (incl. across a looping clip)
                        # must re-alert R-06, so clear the one-per-visit latch here.
                        eng.clear_intrusion(pr, old)
                    # loitering ended: left a zone they were loitering in
                    if old and (pr, old) in loiter_started and confirmed != old:
                        pending_events.append(new_event(
                            LOITERING_END, cfg.camera_id, cfg.site_id, vnow,
                            zone_id=old, person_ref=pr, dwell_time=dwell.dwell(tid, vnow)))
                        loiter_started.discard((pr, old))
                        eng.reset_loiter(pr, old)
                    prev_zone[tid] = confirmed
                d = dwell.update(tid, confirmed, vnow)
                # Consolidated per-track record (age/zones/dwell/confidence) for
                # live overlays, movement records and completed-track summaries.
                registry.observe(tid, cfg.camera_id,
                                 (float(x1), float(y1), float(x2), float(y2)),
                                 float(conf), confirmed, changed, d, pr, vnow)
                zobj = next((z for z in cfg.zones if z.zone_id == confirmed), None)
                if zobj:
                    intr = eng.evaluate_intrusion(pr, confirmed, zobj.restricted, vnow)
                    if intr:
                        pending_alerts.append(intr.as_dict())
                    lo = eng.evaluate_loiter(pr, confirmed, d, vnow,
                                             threshold=loiter_zone.get(confirmed))
                    if lo:
                        pending_alerts.append(lo.as_dict())
                        if (pr, confirmed) not in loiter_started:
                            loiter_started.add((pr, confirmed))
                            pending_events.append(new_event(
                                LOITERING_START, cfg.camera_id, cfg.site_id, vnow,
                                zone_id=confirmed, person_ref=pr, dwell_time=d))

        # Cross-camera identity: embed a budgeted subset of this frame's crops,
        # then ask the API to resolve any track with enough views. Both calls
        # are no-ops when ReID is unavailable.
        if reid.ready:
            reid.observe(frame, tracks, conf_by_tid, vnow,
                         cfg.frame_width, cfg.frame_height)
            reid.resolve_pending(vnow, zone_by_tid)

        # Warn (throttled) when most tracked people fall outside every zone —
        # almost always a polygon that does not reach the frame edge.
        if feet_total >= 50 and (now - last_zone_warn) >= 60.0:
            frac = feet_outside / feet_total
            if frac >= 0.5:
                last_zone_warn = now
                edge = (feet_at_frame_edge / feet_outside) if feet_outside else 0.0
                log.warning(
                    "camera %s: %.0f%% of foot points are in NO zone (%d/%d); "
                    "%.0f%% of those sit near the bottom of the frame — the zone "
                    "polygon probably needs to extend to the bottom edge",
                    cfg.camera_id, frac * 100, feet_outside, feet_total,
                    edge * 100)

        # Refresh what the health post reports. people_in_view needs no zones,
        # so a camera with nothing drawn still says how many people it can see.
        people_in_view = len(tracks)
        people_in_zones = sum(occupancy.values()) if occupancy else 0
        # Feed the smoothing window every processed frame, then drop what has
        # aged out. At process_fps 15 this holds ~22 samples over 1.5s, which is
        # enough for a median to ignore a single-frame dropout.
        live_window.append((vnow, people_in_view, people_in_zones))
        while live_window and vnow - live_window[0][0] > LIVE_WINDOW_SECONDS:
            live_window.popleft()

        det_counts.append(len(tracks))
        processed += 1

        # Evict per-track state for tracks that left the scene (bounded memory).
        stale = reaper.reap(vnow)
        for tid in stale:
            pr = hasher.ref(tid)
            gone_zone = prev_zone.get(tid)
            # emit exit events for a track that vanished while inside a zone
            if gone_zone in restricted_zone_ids:
                pending_events.append(new_event(
                    RESTRICTED_ZONE_EXIT, cfg.camera_id, cfg.site_id, vnow,
                    zone_id=gone_zone, person_ref=pr,
                    duration=round(vnow - restricted_since.pop(tid, vnow), 2)))
            if gone_zone and (pr, gone_zone) in loiter_started:
                pending_events.append(new_event(
                    LOITERING_END, cfg.camera_id, cfg.site_id, vnow,
                    zone_id=gone_zone, person_ref=pr, dwell_time=dwell.dwell(tid, vnow)))
                loiter_started.discard((pr, gone_zone))
            deb.drop(tid)
            dwell.drop(tid)
            reid.drop(tid)          # frees the feature bank + releases the binding
            eng.drop_person(pr)
            prev_zone.pop(tid, None)
            restricted_since.pop(tid, None)
            done = registry.complete(tid)          # final per-track summary
            if done is not None:
                completed_tracks += 1
                log.debug("track %s completed: %s", tid, done.summary())
        if stale:
            log.debug("evicted %d stale track(s); active=%d", len(stale),
                      reaper.active_count())

        # 5s cadence: heartbeat (local record), density events, zone-state, rules
        heartbeat_event = None
        if agg.due(vnow):
            eng.camera.heartbeat(cfg.camera_id, vnow)
            heartbeat_event = new_event(CAMERA_HEARTBEAT, cfg.camera_id, cfg.site_id, vnow)
            for z in cfg.zones:
                occ = occupancy[z.zone_id]
                dens = density_per_sqm(occ, z.area_sqm)
                cap_pct = capacity_pct(occ, z.capacity_max)
                zstats.record(z.zone_id, occ, vnow)
                pending_events.append(new_event(
                    DENSITY_UPDATE, cfg.camera_id, cfg.site_id, vnow,
                    zone_id=z.zone_id, occupancy=occ, density=dens))
                roll = flow.rolling(z.zone_id, vnow)   # 1m rates + net + 5m/15m
                pending_states.append({
                    "zone_id": z.zone_id, "camera_id": cfg.camera_id,
                    "zone_name": z.zone_name, "restricted": z.restricted,
                    "zone_type": z.zone_type,
                    "occupancy": occ, "density": dens, "capacity_pct": cap_pct,
                    "capacity_max": z.capacity_max, "area_sqm": z.area_sqm,
                    "peak_occupancy": zstats.peak(z.zone_id),
                    "avg_occupancy": round(zstats.average(z.zone_id), 1),
                    "trend": zstats.trend(z.zone_id, vnow),
                    "status": density_status(dens, z.warning_density, z.critical_density),
                    "ts": vnow, **roll,
                })
                for al in eng.evaluate_zone(z.zone_id, dens, cap_pct, vnow,
                                            warning_on=z.warning_density,
                                            critical_on=z.critical_density):
                    pending_alerts.append(al.as_dict())
                    if al.rule_id == "R-03" and al.kind == "FIRE":
                        pending_events.append(new_event(
                            CAPACITY_WARNING, cfg.camera_id, cfg.site_id, vnow,
                            zone_id=z.zone_id, occupancy=occ, capacity_pct=cap_pct))

        # --- annotate once, then bookmark this moment if anything happened ---
        # per-track dwell + loiter flags for the feed overlay (Req 13/20)
        track_meta = {t.track_id: {
            "dwell": t.dwell_time,
            "loiter": (t.person_ref, t.current_zone_id) in loiter_started,
            "gref": reid.global_ref(t.track_id),   # None until ReID resolves it
        } for t in registry.active()}
        annotated = annotate(frame.copy(), cfg.zones, tracks, occupancy, track_meta)
        okj, buf = cv2.imencode(".jpg", annotated)
        if okj:
            with _lock:
                _latest_jpeg["buf"] = buf.tobytes()
                # snapshot raw context for toggle-aware re-annotation in the stream
                _render.update(frame=frame, zones=cfg.zones, tracks=list(tracks),
                               occ=dict(occupancy), meta=track_meta)

        # Snapshot only critical density + restricted intrusion — not loitering,
        # density-warning, capacity, or movement events.
        snap_alerts = [a for a in pending_alerts
                       if a.get("rule_id") in SNAPSHOT_RULES and a.get("kind") == "FIRE"]
        frame_ref = None
        if snap_alerts:
            bookmark_seq += 1
            bname = f"bm_{cfg.camera_id}_{bookmark_seq:05d}.jpg"
            cv2.imwrite(os.path.join(BOOKMARKS_DIR, bname), annotated)
            frame_ref = "/bookmarks/" + bname

        # --- pass 2: persist to files + push to API (history + live) ---------
        for ev in pending_events:
            events_fp.write(json.dumps(ev) + "\n")
            if ev["event_type"] in POST_EVENT_TYPES:
                _post("/api/v1/events/ingest", ev)
        if heartbeat_event:
            events_fp.write(json.dumps(heartbeat_event) + "\n")
        for st in pending_states:
            _post("/api/v1/zones/state", st)
        for al in pending_alerts:
            # Rule engine is zone-centric; stamp which camera this alert came from.
            if al.get("camera_id") is None:
                al["camera_id"] = cfg.camera_id
            if frame_ref and al.get("rule_id") in SNAPSHOT_RULES and al.get("kind") == "FIRE":
                al["frame"] = frame_ref
            alerts_fp.write(json.dumps(al) + "\n")
            _post("/api/v1/alerts", al)

        summary = "  ".join(f"{z.zone_name}={occupancy[z.zone_id]}" for z in cfg.zones)
        print(f"[{time.strftime('%H:%M:%S')}] tracked={len(tracks)}  {summary}", flush=True)

        # save an evidence frame ~ every 5s of run
        if elapsed - last_evidence_t >= 5.0:
            last_evidence_t = elapsed
            saved_frames += 1
            p = os.path.join(FRAMES_DIR, f"frame_{slug}_{saved_frames:04d}.jpg")
            cv2.imwrite(p, annotated)
            frame_paths.append(p)

    # finalise evidence
    worker.stop()
    events_fp.close()
    alerts_fp.close()
    build_contact_sheet(frame_paths, os.path.join(EVIDENCE, f"contact_{slug}.jpg"))
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
        "active_tracks": len(registry),
        "completed_tracks": completed_tracks,
        "evidence_frames_saved": saved_frames,
        "camera_health": worker.health(),
        # Counts only — never vectors. "status" says whether cross-camera
        # identity actually ran this session or was unavailable.
        "reid": reid.snapshot(),
        # Zone sanity: a high outside fraction means occupancy will read low
        # even though people are being tracked.
        # Detections discarded by an UNMONITORED mask zone (reflections, screens,
        # posters). A large number here is expected and healthy once a mask is
        # drawn — it is the count of phantoms that did NOT reach occupancy or
        # the identity gallery.
        "ignored_detections": ignored_dets,
        "zone_fit": {
            "foot_points": feet_total,
            "outside_all_zones": feet_outside,
            "outside_pct": round(100.0 * feet_outside / feet_total, 1) if feet_total else 0.0,
            "of_those_on_frame_edge": feet_at_frame_edge,
        },
    }
    with open(os.path.join(EVIDENCE, f"metrics_{slug}.json"), "w") as f:
        json.dump(metrics, f, indent=2)
    print("[info] evidence written to ./evidence/", flush=True)


# --- MJPEG viewer (kept minimal; the dashboard embeds this stream) ---------
def _serve(port=8080):
    from flask import Flask, Response
    app = Flask(__name__)

    @app.route("/")
    def index():
        return '<img src="/stream" style="max-width:100%">'

    @app.route("/stream")
    def stream():
        from flask import request
        # Overlay toggles: ?zones=0&ids=0&feet=0&dwell=0&boxes=0. Absent => default on.
        # With all layers on we serve the pre-encoded default (cheap); any toggle
        # re-annotates the raw frame per-request.
        ov = {k: request.args.get(k, "1") not in ("0", "false", "off")
              for k in OVERLAY_DEFAULT}
        custom = ov != OVERLAY_DEFAULT

        def gen():
            while True:
                buf = None
                if custom:
                    with _lock:
                        r = dict(_render)
                    if r.get("frame") is not None:
                        img = annotate(r["frame"].copy(), r["zones"], r["tracks"],
                                       r["occ"], r["meta"], overlay=ov)
                        okj, enc = cv2.imencode(".jpg", img)
                        if okj:
                            buf = enc.tobytes()
                else:
                    with _lock:
                        buf = _latest_jpeg["buf"]
                if buf is not None:
                    yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + buf + b"\r\n")
                time.sleep(0.05)
        return Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame")

    @app.route("/snapshot")
    def snapshot():
        """ONE annotated JPEG, not a stream.

        Opening an MJPEG stream to grab a single frame is wasteful — it costs a
        held connection and a continuous encode for one image. This returns the
        frame already encoded for the stream, so it is effectively free, and it
        is what makes pushing periodic thumbnails to FinBlade cheap enough to
        do at all (a frame every 30s per camera instead of 20-40 Mbit/s of
        continuous MJPEG).
        """
        from flask import Response as FlaskResponse
        with _lock:
            buf = _latest_jpeg["buf"]
        if buf is None:
            return {"error": "no frame yet"}, 503
        return FlaskResponse(buf, mimetype="image/jpeg")

    @app.route("/health")
    def health():
        w = _worker["ref"]
        return (w.health() if w else {"state": "UNKNOWN"})

    # Demo controls (predictable camera failure/restore) — Req 1.
    @app.route("/simulate-failure", methods=["POST", "GET"])
    def simulate_failure():
        w = _worker["ref"]
        if w:
            w.simulate_failure()
        return {"ok": bool(w), "state": w.state() if w else None}

    @app.route("/restore", methods=["POST", "GET"])
    def restore():
        w = _worker["ref"]
        if w:
            w.restore()
        return {"ok": bool(w), "state": w.state() if w else None}

    app.run(host="0.0.0.0", port=port, threaded=True)


if __name__ == "__main__":
    logging.basicConfig(
        level=os.environ.get("FB_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/cameras.dev.yaml")
    ap.add_argument("--source", default=None,
                    help="override the config's video source (file path or RTSP URL), "
                         "e.g. media/CAM01_S01_normal_entry_exit.mp4")
    ap.add_argument("--camera-id", default=None, help="override the config camera_id")
    ap.add_argument("--site-id", default=None, help="override the config site_id")
    ap.add_argument("--seconds", type=float, default=None,
                    help="stop after N seconds of video (evidence run)")
    ap.add_argument("--no-serve", action="store_true")
    ap.add_argument("--port", type=int, default=8080,
                    help="MJPEG stream port (use a distinct port per camera)")
    ap.add_argument("--stream-host", default="127.0.0.1",
                    help="host the dashboard uses to reach this MJPEG stream")
    ap.add_argument("--api-url", default=None,
                    help="POST live zone-states + alerts to this API base "
                         "(e.g. http://127.0.0.1:8000) for the dashboard")
    args = ap.parse_args()

    if args.api_url:
        _API["base"] = args.api_url.rstrip("/")
        print(f"[info] live-posting to {_API['base']}", flush=True)

    if not args.no_serve:
        _STREAM["url"] = f"http://{args.stream_host}:{args.port}/stream"
        threading.Thread(target=lambda: _serve(args.port), daemon=True).start()
    run(args.config, max_seconds=args.seconds, source=args.source,
        camera_id=args.camera_id, site_id=args.site_id)
