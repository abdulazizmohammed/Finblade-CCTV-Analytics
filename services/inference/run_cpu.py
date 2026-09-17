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

# Credential masking for anything that reaches a log file. Imported from the API
# package so there is one definition; falls back to a no-network-safe local copy
# if this runner is deployed without the API alongside it.
try:
    from services.api.redact import mask_credentials as _mask_credentials
except Exception:                                  # noqa: BLE001
    import re as _re
    _CREDS = _re.compile(r"([a-zA-Z][a-zA-Z0-9+.\-]*://)[^/@:\s]+(?::[^/@\s]*)?@")

    def _mask_credentials(value):
        if not isinstance(value, str) or "@" not in value:
            return value
        return _CREDS.sub(r"\1***:***@", value)

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

from finblade.areas import area_ref                      # noqa: E402
from finblade.config import load_camera_config          # noqa: E402
from finblade.debounce import BoundaryDebouncer          # noqa: E402
from finblade.emission import DensityUpdateGate          # noqa: E402
from finblade.events import (                            # noqa: E402
    CAMERA_HEARTBEAT, CAMERA_OFFLINE, CAMERA_ONLINE, CAMERA_RECOVERED,
    CAPACITY_WARNING, DENSITY_UPDATE, GROUP_CROSSING, HAZARD_FIRE,
    HAZARD_SMOKE, LOITERING_END, LOITERING_START, PERSON_ATTRIBUTES,
    PPE_COMPLIANT, PPE_VIOLATION, RESTRICTED_ZONE_ENTRY, RESTRICTED_ZONE_EXIT,
    WRONG_DIRECTION, ZONE_ENTRY, ZONE_EXIT, ZONE_TRANSITION, new_event,
)
from finblade.geometry import associate_items                   # noqa: E402
from finblade.ppe import (PPEThresholds, PPETracker,            # noqa: E402
                          normalize_profile, status_of as ppe_status_of,
                          evidence_for as ppe_evidence_for, types_for)
from finblade.crowding import (                           # noqa: E402
    CrowdEstimator, TrackingQualityMonitor, select_mode,
)
from finblade.flowrules import (                          # noqa: E402
    DirectionPolicy, GroupCrossingDetector, WrongWayDetector,
)
from finblade.geometry import foot_point                 # noqa: E402
from finblade.attributes import AttributeSampler, Vocabulary, describe   # noqa: E402
from finblade.appearance import CropQualityGate          # noqa: E402
from finblade.identity import PersonRefHasher            # noqa: E402
from finblade.metrics import (                           # noqa: E402
    DwellTracker, FlowCounter, ZoneStateAggregator, ZoneStats, density_per_sqm,
    capacity_pct, density_status,
)
from finblade.rules import SEV_AMBER, Alert, RuleEngine  # noqa: E402
from finblade.tracking import TrackReaper                # noqa: E402
from finblade.tracks import TrackRegistry                # noqa: E402
from finblade.zones import (in_ignored_region, zone_of,      # noqa: E402
                            ppe_requirements, ppe_requirements_rejected)
from services.inference.camera_worker import CameraWorker, CameraState  # noqa: E402
from services.inference.reid_client import ReIDResolver                 # noqa: E402
from services.inference.hazard_client import HazardDetector             # noqa: E402
from services.inference.attr_client import ClipAttributeScorer          # noqa: E402
from services.inference.ppe_client import (PPEDetector,                 # noqa: E402
                                           DEFAULT_MEDICAL_CHECKPOINT,
                                           MEDICAL_CHECKPOINTS)

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

# Report N people if at least (1 - this) of the window saw N or more. 0.75 means
# a quarter of the frames agreeing is enough. See _presence_count.
PRESENCE_QUANTILE = float(os.environ.get("FINBLADE_PRESENCE_QUANTILE", "0.75"))

# Emit the ZONE_EXIT + ZONE_ENTRY pair alongside every ZONE_TRANSITION, so a
# consumer tallying per-zone entries and exits sees both ends of a movement
# without having to unpack transitions itself.
#
# The cost is real and worth stating: on a fully zoned floor most movements are
# transitions, so this roughly TRIPLES movement-event volume. Both extra events
# carry derived=True and the transition remains the authoritative record, so
# nothing that counts movements should count them — but if event storage is the
# binding constraint, this is the switch.
PAIRED_ZONE_EVENTS = os.environ.get("FINBLADE_PAIRED_ZONE_EVENTS", "1") not in (
    "0", "false", "False", "no", "off")


def _presence_count(values, fallback=0, quantile=None):
    """Upper-quantile person count over the window.

    NOT a median, and the reason is measured rather than theoretical. On the
    reception camera a man sitting still was tracked in only 52% of frames, so a
    median sat exactly on the knife-edge and published ZERO people while he was
    plainly sitting there. Raising the publish rate had made that worse, not
    better, because the wrong value now arrived promptly.

    The asymmetry is the whole point. A detection is positive evidence that
    somebody is present. A non-detection is WEAK evidence of absence - it is
    equally consistent with a frame the detector simply missed, which at 640x360
    on a seated subject is roughly half of them. So the estimator has to lean
    toward presence rather than sit in the middle.

    A quantile rather than max() because max lets one phantom frame invent a
    person: this same camera once reported three people from one real one, on
    detections that occupied 0.3% of frames. At the 0.75 default, a count has to
    hold across a quarter of the window before it is published, which admits a
    subject detected half the time and rejects a blip.
    """
    if not values:
        return fallback
    q = PRESENCE_QUANTILE if quantile is None else quantile
    s = sorted(values)
    idx = min(len(s) - 1, int(len(s) * q))
    return int(s[idx])


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

    EVERY EDITABLE FIELD MUST BE LISTED HERE. A field the editor can change and
    this cannot see is an edit the worker never notices: the operator saves, the
    API stores it, the dashboard shows the new value, and the running pipeline
    keeps using the old one indefinitely. There is no error and nothing in the
    log — the change simply does not take.

    That happened to required_ppe. Unticking "safety vest" saved correctly and
    the worker went on raising vest violations, because this signature was
    unchanged and the hot-reload below never ran.
    """
    return json.dumps([[z.get("zone_id"), z.get("zone_type"), z.get("restricted"),
                        z.get("normalized_polygon") or z.get("polygon"),
                        z.get("capacity_max"), z.get("area_sqm"),
                        z.get("warning_density"), z.get("critical_density"),
                        z.get("loitering_threshold_sec"), z.get("enabled"),
                        # Remapping a zone to a different room is an edit like
                        # any other; without it here the worker would keep
                        # stamping the old area id onto its posts.
                        z.get("physical_area_id"),
                        # What this zone demands people wear. Changing it is the
                        # edit most likely to be made DURING a shift, and the
                        # one whose staleness accuses people wrongly.
                        z.get("required_ppe"),
                        # Which vocabulary those items are drawn from. Switching
                        # a zone from industrial to medical changes which model
                        # judges it, so it is as material as the list itself.
                        z.get("ppe_profile")]
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
BGR_FIRE       = (75, 75, 239)    # #ef4b4b fire  — critical, solid
BGR_SMOKE      = (41, 160, 240)   # #f0a029 smoke — warning
BGR_COMPLIANCE = (240, 110, 123)  # #7b6ef0 PPE violation — person-based policy

def _attr_crop(frame, box):
    """The pixels kept with an appearance tag: the person plus a little
    context, copied out of the RAW frame so nothing is burned in. Small on
    purpose — it is evidence for a human confirming a search hit, not a
    still of the room."""
    x1, y1, x2, y2 = box
    h, w = frame.shape[:2]
    pw, ph = (x2 - x1) * 0.15, (y2 - y1) * 0.08
    cx1, cy1 = max(0, int(x1 - pw)), max(0, int(y1 - ph))
    cx2, cy2 = min(w, int(x2 + pw)), min(h, int(y2 + ph))
    if cx2 - cx1 < 16 or cy2 - cy1 < 16:
        return None
    return frame[cy1:cy2, cx1:cx2].copy()


def _stamp_zone_occupancy(events, occupancy, zones) -> None:
    """Add the resulting occupancy/density to movement events, in place.

    A consumer can then rebuild occupancy over time from the event stream alone,
    which is what lets DENSITY_UPDATE — currently one row per zone every 5s,
    duplicating zone_state_ts exactly — stop being emitted.

    ZONE_TRANSITION changes two zones at once, so it carries both: the plain
    fields describe zone_to, and the _from pair describes the origin.
    """
    if not zones:
        return
    areas = {z.zone_id: z.area_sqm for z in zones}

    def _pair(zone_id):
        if zone_id is None or zone_id not in areas:
            return None
        occ = int(occupancy[zone_id])
        return occ, density_per_sqm(occ, areas[zone_id])

    for evt in events:
        et = evt.get("event_type")
        if et == ZONE_TRANSITION:
            here, there = _pair(evt.get("zone_to")), _pair(evt.get("zone_from"))
            if there:
                evt["occupancy_from"], evt["density_from"] = there
        elif et == ZONE_ENTRY:
            here = _pair(evt.get("zone_to"))
        elif et == ZONE_EXIT:
            # "NONE" is the sentinel used when a track leaves without ever
            # having been confirmed in a zone; there is no count to report.
            here = _pair(evt.get("zone_from"))
        else:
            continue
        if here:
            evt["occupancy"], evt["density"] = here


EVIDENCE = os.path.join(_REPO_ROOT, "evidence")
FRAMES_DIR = os.path.join(EVIDENCE, "frames")
BOOKMARKS_DIR = os.path.join(EVIDENCE, "bookmarks")   # saved frame per event/alert

# Movement events pushed to the history store.
ZONE_EVENT_TYPES = {ZONE_ENTRY, ZONE_EXIT, ZONE_TRANSITION}
POST_EVENT_TYPES = {ZONE_ENTRY, ZONE_EXIT, ZONE_TRANSITION, DENSITY_UPDATE,
                    CAPACITY_WARNING, RESTRICTED_ZONE_ENTRY, RESTRICTED_ZONE_EXIT,
                    LOITERING_START, LOITERING_END,
                    # Hazards must reach the store or they cannot appear on the
                    # history page, which is the whole point of raising them.
                    HAZARD_FIRE, HAZARD_SMOKE,
                    PPE_VIOLATION, PPE_COMPLIANT,
                    # Appearance tags exist to be searched, which happens on
                    # the API side; a tag that stays in the worker is nothing.
                    PERSON_ATTRIBUTES}
# Alerts that get a saved snapshot: critical density (R-02) and restricted-zone
# intrusion (R-06) ONLY.
#
# R-05 (loitering) was dropped from this set deliberately. Loitering fires
# continuously for anyone standing still, so on a looping clip it produced 7,741
# frames totalling 944 MB — the evidence directory became the largest thing in
# the repo and the genuinely serious snapshots were buried in it. A snapshot is
# only worth writing when someone must look at it: a red-band crowd density, or
# a person somewhere they are not allowed to be.
SNAPSHOT_RULES = {"R-02", "R-06", "R-10", "R-11"}

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
                   "dwell": True, "gid": True, "hazards": True}


def ppe_maps(zones, camera_id):
    """(requirements, profiles) for every zone that asks for PPE.

    ONE FUNCTION, called from setup AND from the hot-reload, because these two
    maps drifting apart is exactly the bug that let a removed vest requirement
    keep firing: the reload rebuilt the polygons and left the requirements at
    whatever they were when the process started.

    Requirements are filtered to items the zone's own profile recognises. A zone
    can end up with profile=medical and required_ppe=[hardhat] — switch the
    profile after picking items, or hand-edit the YAML — and asking a medical
    checkpoint for a hardhat would judge it on silence: nobody in a pathology
    lab wears one, so everybody would be convicted of not wearing it. Dropping
    it is the safe direction, and it is logged rather than dropped quietly.
    """
    req, prof = {}, {}
    for z in zones:
        rejected = ppe_requirements_rejected(z)
        if rejected:
            log.warning(
                "camera %s zone %s: required_ppe %s are not in the '%s' "
                "profile's vocabulary and will NOT be judged. Either switch "
                "the zone's profile or remove them.",
                camera_id, z.zone_id, rejected,
                normalize_profile(getattr(z, "ppe_profile", None)))
        items = ppe_requirements(z)
        if items:
            req[z.zone_id] = items
            prof[z.zone_id] = normalize_profile(getattr(z, "ppe_profile", None))
    return req, prof


def ppe_served(ppe_zones, ppe_profiles, detectors, camera_id):
    """Drop requirements no LOADED detector can possibly judge.

    THE RULE THIS ENFORCES: an item must not be judged unless a detector that
    can emit its class is actually running. Absence of a detection is evidence
    toward a violation in R-11, so an item nobody is looking for does not come
    out UNKNOWN — it comes out NONCOMPLIANT, and accuses every person in the
    zone of not wearing something no model was ever asked about.

    Observed live: a zone switched to the medical profile while only the
    industrial detector was loaded. The industrial model cannot emit
    surgical_mask or surgical_gloves, so both read "absent" on every tick and
    the zone reported 2 non-compliant people with violations for both items.
    Entirely fabricated — there was no medical model on the machine at all.

    Filtering here rather than in the judging loop means the zone-state counts,
    the alert path and the evidence file all see the same filtered set, so the
    card and the feed cannot disagree about it.
    """
    live = {d.profile for d in detectors if d.enabled}
    # ITEM granularity on top of profile granularity. A loaded detector does
    # not necessarily carry every class in its profile's vocabulary — the lab
    # checkpoint speaks five of the medical profile's ten items — and the
    # missing five would be judged on silence just as surely as a missing
    # model. A detector that publishes `served_types` is held to it; one that
    # does not (older adapters, test stubs) is taken to serve its whole
    # profile, which is the behaviour this function had before.
    served = {}
    for d in detectors:
        if not d.enabled:
            continue
        types = getattr(d, "served_types", None)
        served.setdefault(d.profile, set()).update(
            types_for(d.profile) if types is None else types)
    kept, kept_prof, dropped, partial = {}, {}, {}, {}
    for zid, items in ppe_zones.items():
        prof = ppe_profiles.get(zid)
        if prof not in live:
            dropped[zid] = (prof, items)
            continue
        ok = [i for i in items if i in served.get(prof, set())]
        missing = [i for i in items if i not in served.get(prof, set())]
        if missing:
            partial[zid] = (prof, missing)
        if ok:
            kept[zid] = ok
            kept_prof[zid] = prof
    for zid, (prof, items) in dropped.items():
        log.warning(
            "camera %s zone %s: requires %s on the '%s' profile, but NO '%s' "
            "detector is loaded — those items are NOT being judged. Nobody is "
            "reported non-compliant for them, which is the only honest "
            "outcome; judging them would convict everyone on the detector's "
            "absence. Load that model (or restart this camera if you have just "
            "enabled it) to make the requirement real.",
            camera_id, zid, items, prof, prof)
    for zid, (prof, missing) in partial.items():
        log.warning(
            "camera %s zone %s: requires %s, but the loaded '%s' checkpoint "
            "has no class for them — those items are NOT being judged. The "
            "checkpoint serves %s. Either drop the requirement or load a "
            "model that can see it.",
            camera_id, zid, missing, prof, sorted(served.get(prof, ())))
    return kept, kept_prof


def ppe_state_fields(zone_id, occupancy, ppe_zones, counts, camera_id):
    """PPE compliance fields for one zone's state payload, invariant-checked.

    compliant + non_compliant + not_assessable MUST equal occupancy: the card
    shows them beside each other and a reader will assume they add up.

    THE CHECK IS HERE BECAUSE I GOT THIS WRONG TWICE. First by caching the
    counts from the 2 Hz detector tick, so they described a moment up to 5s
    before the occupancy beside them. Then by rebuilding from
    registry.active(), which includes tracks the reaper keeps alive after they
    have left frame — 7+5+1=13 against an occupancy of 8. Both times a single
    spot-check happened to agree and I called it fixed.

    Logging rather than raising: a miscounted dashboard tile must not take a
    camera down. But it must not be invisible either, which sampling by hand
    plainly was.
    """
    c = counts.get(zone_id) or {"compliant": 0, "non_compliant": 0,
                                "not_assessable": 0, "violations": {}}
    total = c["compliant"] + c["non_compliant"] + c["not_assessable"]
    if total != occupancy:
        log.warning(
            "camera %s zone %s: PPE counts do not add up — %d compliant + %d "
            "non-compliant + %d not-assessable = %d, but occupancy is %d. The "
            "zone card will show numbers that disagree; the counts and the "
            "occupancy are being taken from different sources.",
            camera_id, zone_id, c["compliant"], c["non_compliant"],
            c["not_assessable"], total, occupancy)
    return {"ppe_required": list(ppe_zones[zone_id]),
            "ppe_compliant": c["compliant"],
            "ppe_non_compliant": c["non_compliant"],
            "ppe_not_assessable": c["not_assessable"],
            "ppe_violations": dict(c["violations"])}


def _draw_hazards(frame, hazards):
    """Fire/smoke boxes. Drawn FIRST so person boxes sit on top of them.

    A hazard region is usually large — a smoke plume can cover a third of the
    frame — so drawing it last would bury the people underneath it. Thicker
    stroke than a track box because it is the reason the frame was saved.
    """
    for x1, y1, x2, y2, name, conf in hazards or ():
        colour = BGR_FIRE if name == "fire" else BGR_SMOKE
        p1 = (int(x1), int(y1))
        p2 = (int(x2), int(y2))
        cv2.rectangle(frame, p1, p2, colour, 3)
        label = "%s %.2f" % (name.upper(), conf)
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 2)
        # Label INSIDE the box when it would otherwise fall off the top edge —
        # a smoke box often starts at y=0 and the text would be clipped away.
        ty = p1[1] - 8 if p1[1] > th + 12 else p1[1] + th + 8
        cv2.rectangle(frame, (p1[0], ty - th - 6), (p1[0] + tw + 8, ty + 4),
                      colour, -1)
        cv2.putText(frame, label, (p1[0] + 4, ty),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (16, 16, 16), 2)


def annotate(frame, zones, tracks, occupancy, track_meta=None, overlay=None,
             hazards=None):
    track_meta = track_meta or {}
    o = dict(OVERLAY_DEFAULT)
    if overlay:
        o.update(overlay)
    if o.get("hazards", True):
        _draw_hazards(frame, hazards)
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
        # PPE verdict for this track, if compliance is being judged at all.
        # "missing" is a list of items; empty means compliant, None means the
        # person is not assessable (too small in frame) or PPE is off here.
        ppe_missing = meta.get("ppe_missing")
        # Priority: restricted intrusion (red) > loitering (amber) >
        # PPE violation (amber) > not assessable (grey) > normal (teal).
        #
        # Intrusion outranks PPE because it is an incident and PPE is a state;
        # an operator seeing one box should be told the more urgent thing about
        # it. Not-assessable is drawn in the same muted grey as a masked
        # detection, because it means the same thing: present, deliberately not
        # being judged. Teal stays chrome and only ever means "nothing to say".
        if in_restricted:
            box_color, tag = BGR_CRITICAL, "INTRUSION"
        elif loiter:
            box_color, tag = BGR_WARNING, "LOITERING"
        elif ppe_missing:
            # Violet, matching the alert feed and history. Amber here would have
            # made a worker without a hardhat look identical to a zone filling
            # up, on the one surface where telling them apart matters most.
            box_color = BGR_COMPLIANCE
            tag = "NO " + "/".join(
                {"hardhat": "HAT", "safety_vest": "VEST",
                 "mask": "MASK"}.get(m, m.upper()) for m in ppe_missing)
        elif ppe_missing is None and meta.get("ppe_judged") is False:
            box_color, tag = BGR_IGNORED, "PPE n/a"
        else:
            box_color, tag = BGR_TRACK, ""
        thick = 3 if (in_restricted or loiter or ppe_missing) else 2
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
        # NEVER log the raw source. It is normally rtsp://user:password@host and
        # this line lands in scripts/cam_<id>.log in cleartext, where the secret
        # unlocks the camera itself rather than this service.
        log.info("source overridden to %s", _mask_credentials(source))
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
            print(f"[BLOCKER] cannot open source: {_mask_credentials(src)}",
                  file=sys.stderr)
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
    density_gate = DensityUpdateGate(
        os.environ.get("FINBLADE_DENSITY_UPDATE_MODE", "threshold"))
    if density_gate.invalid_mode:
        log.warning("FINBLADE_DENSITY_UPDATE_MODE=%r is not one of "
                    "threshold/always/off — using 'threshold'",
                    density_gate.invalid_mode)
    log.info("DENSITY_UPDATE emission: %s", density_gate.mode)
    eng = RuleEngine()
    hasher = PersonRefHasher()
    # Direction and group rules read the confirmed transition stream, so they
    # inherit the boundary debounce for free: jitter never reaches them.
    wrongway = WrongWayDetector(DirectionPolicy.from_zones(
        [z.to_dict() for z in cfg.zones]))
    group_rule = GroupCrossingDetector()
    group_cfg = {z.zone_id: {"threshold": z.group_threshold,
                             "window_s": z.group_window_s}
                 for z in cfg.zones if getattr(z, "group_threshold", 0)}
    occ_threshold = {z.zone_id: z.occupancy_threshold for z in cfg.zones
                     if getattr(z, "occupancy_threshold", 0)}
    quality = TrackingQualityMonitor()
    # No crowd-counting model ships here and none can be fetched (air-gapped,
    # pinned dependencies). The seam exists so one can be registered; until then
    # a saturated scene is reported as degraded rather than silently handed to a
    # method that does not exist.
    crowd_model = CrowdEstimator()
    last_quality = None
    reid_zone_ids = {z.zone_id for z in cfg.zones if getattr(z, "reid", False)}
    if reid_zone_ids:
        log.info("camera %s: cross-camera ReID restricted to %s",
                 cfg.camera_id, ", ".join(sorted(reid_zone_ids)))
    if wrongway.policy.policed_pairs():
        log.info("camera %s: one-way routes policed: %s", cfg.camera_id,
                 ", ".join(f"{a}->{b}" for a, b in wrongway.policy.policed_pairs()))
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
    # Fire/smoke: a second model on the SAME decoded frame, sampled at 2 Hz
    # rather than per frame (see hazard_client for the measured reason). Off
    # unless the config asks for it, and it disables itself loudly rather than
    # faking a reading if the weights are missing.
    _hz_cfg = getattr(cfg, "hazard", None) or {}
    hazard = HazardDetector(
        cfg.camera_id,
        weights=_hz_cfg.get("weights"),
        device=str(_hz_cfg.get("device", "0")),
        interval_s=float(_hz_cfg.get("interval_seconds", 0.5)),
        conf_threshold=float(_hz_cfg.get("conf_threshold", 0.30)),
        imgsz=int(_hz_cfg.get("imgsz", 640)),
        enabled=bool(_hz_cfg.get("enabled", False)),
    )
    hazard.load()
    # Appearance attributes for search (finblade/attributes.py). A fourth model
    # — CLIP zero-shot — but scored on a handful of crops per track in its
    # whole life, not per frame, so its cost is a rounding error next to the
    # detector. Off unless the config asks; disables itself loudly without the
    # weights. The crop gate is the ReID one: a truncated or occluded person
    # produces a confident wrong colour exactly as it produces a bad embedding.
    _at_cfg = getattr(cfg, "attributes", None) or {}
    _at_vocab = Vocabulary.from_config(_at_cfg)
    attr_scorer = ClipAttributeScorer(
        _at_vocab, weights=_at_cfg.get("weights"),
        device=str(_at_cfg.get("device", "0")),
        enabled=bool(_at_cfg.get("enabled", False)),
        half=bool(_at_cfg.get("half", True)))
    attr_scorer.load()
    attrs = AttributeSampler(
        _at_vocab, attr_scorer.score,
        gate=CropQualityGate(min_height_px=float(_at_cfg.get("min_crop_height", 96.0)),
                             min_confidence=float(_at_cfg.get("min_crop_confidence", 0.5))),
        samples=int(_at_cfg.get("samples", 3)),
        sample_interval_s=float(_at_cfg.get("sample_interval_seconds", 3.0)),
        stable_age_s=float(_at_cfg.get("stable_age_seconds", 2.0)),
        min_confidence=float(_at_cfg.get("min_confidence", 0.45)),
        budget_per_frame=int(_at_cfg.get("budget_per_frame", 4)),
        crop_fn=_attr_crop)
    attr_counter = [0]              # crops saved; a list so the closure can bump it

    def _emit_attribute_tag(tag, zone_id):
        """One PERSON_ATTRIBUTES event per track, with its best crop saved.

        person_ref and global_ref are the same anonymous refs every other event
        carries, so a search hit joins to the rest of that person's movement
        without any new identifier. The crop is the raw pixels at the best
        sample — saved so a human can confirm a hit, and subject to the same
        retention as every other bookmark.
        """
        frame_ref = None
        if tag.crop is not None and getattr(tag.crop, "size", 0) > 0:
            attr_counter[0] += 1
            _name = f"attr_{cfg.camera_id}_{attr_counter[0]:06d}_t{tag.track_id}.jpg"
            try:
                cv2.imwrite(os.path.join(BOOKMARKS_DIR, _name), tag.crop)
                frame_ref = "/bookmarks/" + _name
            except Exception:                               # noqa: BLE001
                log.exception("attribute crop save failed")
        who = dict(person_ref=hasher.ref(tag.track_id), track_id=tag.track_id)
        _g = reid.global_ref(tag.track_id)
        if _g:
            who["global_ref"] = _g
        ev = new_event(PERSON_ATTRIBUTES, cfg.camera_id, cfg.site_id, tag.ts,
                       attributes=tag.attributes, confidences=tag.confidence,
                       samples=tag.samples, description=describe(tag.attributes),
                       **who)
        if zone_id:
            ev["zone_id"] = zone_id
        if frame_ref:
            ev["frame"] = frame_ref
        pending_events.append(ev)
    # PPE compliance (R-11). Third model, same frame, same cadence discipline.
    # Off unless the config asks. Zone-scoped: if no zone declares required_ppe
    # there is nothing to judge and the model is not loaded at all, so a site
    # that does not do PPE pays nothing for the feature existing.
    _ppe_cfg = getattr(cfg, "ppe", None) or {}
    _med_cfg = getattr(cfg, "medical_ppe", None) or {}
    _ppe_zones, _ppe_profiles = ppe_maps(cfg.zones, cfg.camera_id)

    def _wants(profile):
        """Does any zone on this camera use that vocabulary? A detector whose
        profile nobody asks for is never loaded, so a lab pays nothing for the
        industrial model existing and a building site pays nothing for the
        medical one."""
        return any(p == profile for p in _ppe_profiles.values())

    ppe = PPEDetector(
        cfg.camera_id,
        weights=_ppe_cfg.get("weights"),
        device=str(_ppe_cfg.get("device", "0")),
        interval_s=float(_ppe_cfg.get("interval_seconds", 0.5)),
        conf_threshold=float(_ppe_cfg.get("conf_threshold", 0.35)),
        imgsz=int(_ppe_cfg.get("imgsz", 640)),
        enabled=bool(_ppe_cfg.get("enabled", False)) and _wants("industrial"),
    )
    ppe.load()
    # SECOND, INDEPENDENT DETECTOR. Same class, different vocabulary — not a
    # second rule engine. Off unless medical_ppe.enabled AND some zone declares
    # ppe_profile: medical. If its weights are absent, load() disables it with
    # a clear message and every other rule carries on; that is the same
    # failure path the industrial detector uses.
    #
    # WHICH checkpoint fills the slot is one config word, `checkpoint`, looked
    # up in MEDICAL_CHECKPOINTS — the class map, default weights and the
    # class-order assertion come as a set. An unknown name is a config error
    # and is treated like missing weights: disabled loudly, nothing judged.
    _med_name = str(_med_cfg.get("checkpoint", DEFAULT_MEDICAL_CHECKPOINT))
    _med_ck = MEDICAL_CHECKPOINTS.get(_med_name)
    if _med_ck is None:
        log.error("camera %s: medical_ppe.checkpoint %r is not one of %s; "
                  "medical PPE DISABLED", cfg.camera_id, _med_name,
                  sorted(MEDICAL_CHECKPOINTS))
        _med_ck = MEDICAL_CHECKPOINTS[DEFAULT_MEDICAL_CHECKPOINT]
        _med_cfg = dict(_med_cfg, enabled=False)
    ppe_med = PPEDetector(
        cfg.camera_id,
        weights=_med_cfg.get("weights") or _med_cfg.get("model_path")
        or _med_ck["weights"],
        device=str(_med_cfg.get("device", _ppe_cfg.get("device", "0"))),
        # Same cadence as the industrial detector by default, deliberately: the
        # two run on the same tick so neither contributes "absent" evidence
        # merely because the other one happened to run this frame.
        interval_s=float(_med_cfg.get("interval_seconds",
                                      _ppe_cfg.get("interval_seconds", 0.5))),
        # 0.5, not the industrial 0.35: the lab checkpoint's test-split
        # precision is 0.20, and everything user-visible has to sit well
        # above where it guesses. Raise it before you lower it.
        conf_threshold=float(_med_cfg.get("conf_threshold", 0.5)),
        iou_threshold=(float(_med_cfg["iou_threshold"])
                       if _med_cfg.get("iou_threshold") is not None else None),
        imgsz=int(_med_cfg.get("imgsz", 640)),
        enabled=bool(_med_cfg.get("enabled", False)) and _wants("medical"),
        class_map=_med_ck["class_map"],
        ignored_classes=_med_ck["ignored_classes"],
        expected_names=_med_ck["expected_names"],
        profile="medical",
        # Raw journal so precision can be measured on real footage. Off unless
        # the config names a directory; `{camera}` is not needed — the file is
        # <dir>/<camera_id>.jsonl.
        raw_log_dir=(os.path.join(_REPO_ROOT, str(_med_cfg["raw_log_dir"]))
                     if _med_cfg.get("raw_log_dir") else None),
        raw_log_conf=(float(_med_cfg["raw_log_conf"])
                      if _med_cfg.get("raw_log_conf") is not None else None),
    )
    ppe_med.load()
    # Only the ones that actually came up. detect() on a not-ready detector
    # returns [] rather than raising, but keeping the list tight means the
    # per-frame loop does no work for a model that is not there.
    _ppe_detectors = [d for d in (ppe, ppe_med) if d.enabled]
    # A requirement no loaded detector can judge must be dropped, not judged on
    # silence. Applied here AND on every hot-reload, because either the zones or
    # the detector set can change underneath the other.
    _ppe_zones, _ppe_profiles = ppe_served(_ppe_zones, _ppe_profiles,
                                           _ppe_detectors, cfg.camera_id)
    if ppe_med.enabled:
        log.warning(
            "camera %s: MEDICAL PPE is an EVALUATION capability — the "
            "checkpoint (%s) is a first-pass model on 226 training images "
            "with test mAP50 0.22 / precision 0.20, unvalidated on this "
            "site's footage; surgical_gloves is EXPERIMENTAL, and this is "
            "not a certified safety system. AI-assisted monitoring only; "
            "site validation required.", cfg.camera_id, _med_name)
    ppe_tracker = PPETracker(cfg.camera_id, PPEThresholds(
        entry_grace_s=float(_ppe_cfg.get("entry_grace_seconds", 5.0)),
        violation_confirm_s=float(_ppe_cfg.get("violation_confirm_seconds", 8.0)),
        recovery_confirm_s=float(_ppe_cfg.get("recovery_confirm_seconds", 5.0)),
        min_confidence=float(_ppe_cfg.get("min_confidence", 0.40)),
        absence_weight=float(_ppe_cfg.get("absence_weight", 0.25)),
        min_person_height_px=float(_ppe_cfg.get("min_person_height_px", 120.0)),
        # Medical items convict on EXPLICIT negatives only by default. The
        # lab checkpoint's recall is 0.32, and at conf 0.5 it produced no
        # Gloves / Haircap / Mask positive at all on its own test images —
        # silence is its normal output, so any weight on silence convicts
        # the whole room. Set medical_ppe.absence_weight to override.
        absence_weight_by_profile={
            "medical": float(_med_cfg.get("absence_weight", 0.0))}))
    ppe_last_seen = {}          # track_id -> last tick, for the state machine's dt
    # Per-zone people counts, refreshed on each PPE tick and read by the
    # zone-state payload. Held between ticks because the detector runs at 2 Hz
    # while zone state is posted every 5s — without this the counts would be
    # whatever happened to be true on the exact frame the post landed on.
    ppe_zone_counts = {}
    ppe_roll = []
    if _ppe_zones:
        log.info("camera %s: PPE zones %s", cfg.camera_id, _ppe_zones)
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
                    # Same reason the rule latches go: after a gap, the next
                    # tick must re-establish each zone's density status rather
                    # than suppress it as unchanged against a reading from
                    # before the camera dropped.
                    density_gate.reset()
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
            hv["people_in_view"] = _presence_count(
                [w[1] for w in live_window], people_in_view)
            hv["people_in_zones"] = _presence_count(
                [w[2] for w in live_window], people_in_zones)
            # Whether those counts can be believed. Detect-and-track degrades
            # silently in a crowd — an occupancy of 40 in a space holding 90
            # looks exactly like an occupancy of 40 in a space holding 40 — so
            # the count travels with its own reliability.
            hv.update(quality.snapshot())
            hv["counting_mode"] = select_mode(hv["tracking_quality"], crowd_model)
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
                    # EVERY MAP DERIVED FROM cfg.zones HAS TO BE REBUILT HERE.
                    # _ppe_zones was not, so a reload swapped the polygons while
                    # the PPE requirements stayed frozen at whatever they were
                    # when the process started.
                    _prev_ppe = _ppe_zones
                    _prev_prof = _ppe_profiles
                    _ppe_zones, _ppe_profiles = ppe_maps(cfg.zones, cfg.camera_id)
                    # Same filter as at startup: a profile switch can point a
                    # zone at a detector that is not loaded, and judging it
                    # then would convict everyone on that model's absence.
                    _ppe_zones, _ppe_profiles = ppe_served(
                        _ppe_zones, _ppe_profiles, _ppe_detectors, cfg.camera_id)
                    if _ppe_zones != _prev_ppe or _ppe_profiles != _prev_prof:
                        # A verdict already reached for an item nobody requires
                        # any more must go, or zone_summary keeps counting that
                        # person non-compliant: it scans every state held for the
                        # track, not just the ones still being asked for. The
                        # alert stops immediately; without this the ZONE CARD
                        # would disagree with the alert feed until the track
                        # left frame.
                        _keep = {r for reqs in _ppe_zones.values() for r in reqs}
                        _forgotten = ppe_tracker.retain_types(_keep)
                        log.info("camera %s: PPE requirements now %s (was %s); "
                                 "dropped %d stale verdict(s)", cfg.camera_id,
                                 _ppe_zones or "none", _prev_ppe or "none",
                                 _forgotten)
                        # The detector is loaded once, gated on there being at
                        # least one PPE zone. Adding the FIRST requirement to a
                        # camera that started with none therefore cannot take
                        # effect until it restarts — say so plainly rather than
                        # leaving an operator watching for alerts that no model
                        # is running to produce.
                        for _p, _det in (("industrial", ppe), ("medical", ppe_med)):
                            if any(v == _p for v in _ppe_profiles.values()) \
                                    and not _det.enabled:
                                log.warning(
                                    "camera %s: zones now use the '%s' PPE "
                                    "profile but that model was not loaded at "
                                    "startup (no zone asked for it). RESTART "
                                    "THIS CAMERA for those items to be judged; "
                                    "nothing is being checked until you do.",
                                    cfg.camera_id, _p)
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

        # ADOPT THE REAL GEOMETRY FROM A REAL FRAME, not just from the probe at
        # startup. The probe opens the source before anything else happens, and
        # if the stream is not publishing yet it returns 0x0 and is skipped -
        # leaving cfg.frame_width/height at whatever the YAML guessed. Zones are
        # normalized 0-1, so the wrong denominator puts every polygon in the
        # wrong place: with a 1280x720 config against a 2688x1520 source they
        # land squeezed into the top-left 48% of the picture, which reads as
        # "the editor saved my zones wrong" and sends you to the wrong file.
        #
        # It happens whenever the workers start BEFORE the cameras publish,
        # which is exactly what you want to do when the sources are recordings
        # you need to begin in step. So take the size from a frame we actually
        # decoded, and rebuild the zones if it disagrees.
        _fh, _fw = frame.shape[:2]
        if _fw > 0 and _fh > 0 and (_fw, _fh) != (cfg.frame_width, cfg.frame_height):
            log.warning("%s: frames are %dx%d but zones were scaled to %dx%d - "
                        "rebuilding zones against the real geometry",
                        cfg.camera_id, _fw, _fh, cfg.frame_width, cfg.frame_height)
            cfg.frame_width, cfg.frame_height = _fw, _fh
            _raw_now = _fetch_zones_raw(cfg.camera_id)
            cfg.zones = (_zones_from_raw(_raw_now, _fw, _fh)
                         if _raw_now else list(config_zones))
            restricted_zone_ids = {z.zone_id for z in cfg.zones if z.restricted}
            loiter_zone = {z.zone_id: z.loitering_threshold_sec for z in cfg.zones}
            zone_sig = _zone_sig(_raw_now) if _raw_now else None

        if not last_still:
            # The zone editor draws on this still, so if it never lands the
            # operator cannot create zones at all — and without zones there is no
            # occupancy, no density and no rules. Worth getting right.
            #
            # cv2.imwrite returns False rather than raising when the directory is
            # missing, so the old `except` never fired: on a fresh clone (nothing
            # under media/ is tracked) the write failed silently, last_still was
            # set anyway so it never retried, and the editor hung with no error
            # logged anywhere. Create the directory, check the result, and only
            # stop retrying once a frame is actually on disk.
            still_dir = os.path.join(_REPO_ROOT, "media")
            try:
                os.makedirs(still_dir, exist_ok=True)
                ok = cv2.imwrite(os.path.join(still_dir, f"{slug}_frame.jpg"), frame)
            except Exception as exc:
                ok = False
                log.warning("could not save reference still for %s: %s",
                            cfg.camera_id, exc)
            if ok:
                last_still = True
            else:
                log.warning("reference still for %s not written to %s — the zone "
                            "editor will have no frame to draw on",
                            cfg.camera_id, still_dir)

        res = model.track(frame, persist=True, classes=[cfg.person_class_id],
                          conf=cfg.conf_threshold, iou=cfg.iou, imgsz=cfg.imgsz,
                          max_det=cfg.max_det, tracker="bytetrack.yaml",
                          device=device, verbose=False)[0]

        # --- pass 1: detect -> zone -> occupancy; collect events/alerts ------
        tracks = []
        conf_by_tid = {}     # tid -> detection confidence (ReID crop gating)
        zone_by_tid = {}     # tid -> confirmed zone (stamped on the identity)
        occupancy = {z.zone_id: 0 for z in cfg.zones}
        # WHO is in each zone, not just how many. A count cannot be
        # de-duplicated across cameras after the fact — two cameras each
        # reporting "1" could be one person seen twice or two people, and
        # nothing downstream can tell which. Sending identities lets the API
        # count distinct people per physical area (see finblade/areas.py).
        zone_occupants = {z.zone_id: set() for z in cfg.zones}
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
                    # area_ref falls back to a CAMERA-SCOPED key until ReID
                    # resolves a global ref, so two unresolved people are never
                    # merged by both happening to be track 17.
                    zone_occupants[confirmed].add(
                        area_ref(cfg.camera_id, tid, reid.global_ref(tid)))
                pr = hasher.ref(tid)
                if changed:
                    old = prev_zone.get(tid)
                    # The detector's own confidence for the box that produced
                    # this event, so a consumer can discount a marginal
                    # detection. This was a hard-coded 0.9, which told a
                    # consumer nothing and looked like a measurement.
                    det_conf = round(float(conf_by_tid.get(tid, 0.0)), 4)
                    who = dict(person_ref=pr, track_id=tid, confidence=det_conf)
                    # The cross-camera identity, when ReID has resolved one.
                    # Omitted rather than sent as null: absent means "not
                    # resolved", which is a different statement from "resolved
                    # to nothing", and the schema treats it that way.
                    gref = reid.global_ref(tid)
                    if gref:
                        who["global_ref"] = gref
                    if old and confirmed:
                        # A confirmed move between zones is ONE movement, and
                        # ZONE_TRANSITION is its authoritative record. The
                        # exit/entry pair is emitted alongside it for consumers
                        # that tally per-zone entries and exits, marked derived
                        # so nothing counts the movement twice.
                        if PAIRED_ZONE_EVENTS:
                            pending_events.append(new_event(
                                ZONE_EXIT, cfg.camera_id, cfg.site_id, vnow,
                                zone_from=old, derived=True, **who))
                            pending_events.append(new_event(
                                ZONE_ENTRY, cfg.camera_id, cfg.site_id, vnow,
                                zone_to=confirmed, derived=True, **who))
                        pending_events.append(new_event(
                            ZONE_TRANSITION, cfg.camera_id, cfg.site_id, vnow,
                            zone_from=old, zone_to=confirmed, **who))
                        # A transition is outflow from one zone and inflow to
                        # the other. Recording neither left per-zone flow rates
                        # blind to every movement that did not start or end
                        # outside all zones — which on a fully zoned floor is
                        # nearly all of them.
                        flow.record_exit(old, vnow)
                        flow.record_entry(confirmed, vnow)
                        # Wrong way (REQ-23): only pairs an operator declared.
                        wv = wrongway.check(pr, old, confirmed, vnow)
                        if wv:
                            pending_events.append(new_event(
                                WRONG_DIRECTION, cfg.camera_id, cfg.site_id, vnow,
                                zone_from=old, zone_to=confirmed, **who))
                            pending_alerts.append(Alert(
                                "R-10", SEV_AMBER,
                                f"wrong-way movement {old} -> {confirmed} "
                                f"(allowed {wv['allowed_direction']})",
                                vnow, zone_id=confirmed, person_ref=pr,
                                camera_id=cfg.camera_id).as_dict())
                    elif confirmed:
                        pending_events.append(new_event(
                            ZONE_ENTRY, cfg.camera_id, cfg.site_id, vnow,
                            zone_to=confirmed, **who))
                        flow.record_entry(confirmed, vnow)
                    else:
                        pending_events.append(new_event(
                            ZONE_EXIT, cfg.camera_id, cfg.site_id, vnow,
                            zone_from=old or "NONE", **who))
                        if old:
                            flow.record_exit(old, vnow)
                    # restricted-zone entry / exit events
                    if confirmed in restricted_zone_ids:
                        restricted_since[tid] = vnow
                        pending_events.append(new_event(
                            RESTRICTED_ZONE_ENTRY, cfg.camera_id, cfg.site_id, vnow,
                            zone_id=confirmed, **who))
                    if old in restricted_zone_ids and confirmed not in restricted_zone_ids:
                        pending_events.append(new_event(
                            RESTRICTED_ZONE_EXIT, cfg.camera_id, cfg.site_id, vnow,
                            zone_id=old,
                            duration=round(vnow - restricted_since.pop(tid, vnow), 2),
                            **who))
                        # a "visit" ended -> re-entry (incl. across a looping clip)
                        # must re-alert R-06, so clear the one-per-visit latch here.
                        eng.clear_intrusion(pr, old)
                    # loitering ended: left a zone they were loitering in
                    if old and (pr, old) in loiter_started and confirmed != old:
                        pending_events.append(new_event(
                            LOITERING_END, cfg.camera_id, cfg.site_id, vnow,
                            zone_id=old, dwell_time=dwell.dwell(tid, vnow), **who))
                        loiter_started.discard((pr, old))
                        eng.reset_loiter(pr, old)
                    # Group crossing (REQ-24): distinct people arriving in one
                    # zone inside a window. Counted on ARRIVAL, so it covers a
                    # transition and a first entry alike.
                    if confirmed in group_cfg:
                        grp = group_rule.record(confirmed, pr, vnow, group_cfg)
                        if grp:
                            pending_events.append(new_event(
                                GROUP_CROSSING, cfg.camera_id, cfg.site_id, vnow,
                                zone_id=confirmed, count=grp["count"],
                                window_s=grp["window_s"]))
                            pending_alerts.append(Alert(
                                "R-11", SEV_AMBER,
                                f"{grp['count']} people entered {confirmed} "
                                f"within {grp['window_s']:.0f}s",
                                vnow, zone_id=confirmed,
                                camera_id=cfg.camera_id).as_dict())
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
                                zone_id=confirmed, person_ref=pr, track_id=tid,
                                confidence=round(float(conf_by_tid.get(tid, 0.0)), 4),
                                dwell_time=d))

        # Cross-camera identity: embed a budgeted subset of this frame's crops,
        # then ask the API to resolve any track with enough views. Both calls
        # are no-ops when ReID is unavailable.
        if reid.ready:
            # Selective ReID: if any zone opts in, only people standing in those
            # zones are embedded and matched. Nothing opts in -> unchanged
            # behaviour, so an existing deployment is not silently altered.
            if reid_zone_ids:
                reid_tracks = [t for t in tracks
                               if zone_by_tid.get(t[0]) in reid_zone_ids]
            else:
                reid_tracks = tracks
            if reid_tracks:
                reid.observe(frame, reid_tracks, conf_by_tid, vnow,
                             cfg.frame_width, cfg.frame_height)
            reid.resolve_pending(vnow, zone_by_tid)

        # Appearance tags: score a few crops per track, emit once per track.
        # The crop saved with the tag is cut from the RAW frame (no boxes or
        # labels burned in) at the sampler's best-scoring box, so a human
        # confirming a search hit sees the person, not the annotation.
        if attr_scorer.ready and tracks:
            for _tg in attrs.observe(frame, tracks, conf_by_tid, vnow,
                                     cfg.frame_width, cfg.frame_height):
                _emit_attribute_tag(_tg, zone_by_tid.get(_tg.track_id))

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
        # Detection quality for this frame. Uses only what the pipeline already
        # produced — no second model, no extra inference.
        quality.observe(vnow, list(conf_by_tid), list(conf_by_tid.values()),
                        max_det=cfg.max_det)
        q_now = quality.assess()
        if q_now != last_quality:
            last_quality = q_now
            if q_now != "RELIABLE":
                log.warning(
                    "camera %s: tracking quality %s (mean conf %.2f, churn "
                    "%.1f/min, detector saturation %.0f%%) — occupancy is "
                    "likely an UNDERCOUNT while this holds",
                    cfg.camera_id, q_now, quality.mean_confidence(),
                    quality.churn_per_person_per_min(),
                    quality.saturation_fraction() * 100)
            else:
                log.info("camera %s: tracking quality back to RELIABLE",
                         cfg.camera_id)

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
            # A track reaped while still inside a zone used to emit a restricted
            # exit and a loitering end, but never a plain ZONE_EXIT. The zone's
            # occupancy dropped anyway, because the count is rebuilt from live
            # tracks each frame — so zone_state_ts stayed right and nothing
            # looked wrong. The EVENT STREAM was missing the decrement, which
            # only matters once the stream is the source of truth: reconstructed
            # occupancy would climb and never come down.
            # No detection produced these — the track is gone, so there is no
            # box and no confidence to report. track_id still applies.
            gone_who = dict(person_ref=pr, track_id=tid)
            gone_gref = reid.global_ref(tid)
            if gone_gref:
                gone_who["global_ref"] = gone_gref
            if gone_zone:
                pending_events.append(new_event(
                    ZONE_EXIT, cfg.camera_id, cfg.site_id, vnow,
                    zone_from=gone_zone, **gone_who))
                flow.record_exit(gone_zone, vnow)
            # emit exit events for a track that vanished while inside a zone
            if gone_zone in restricted_zone_ids:
                pending_events.append(new_event(
                    RESTRICTED_ZONE_EXIT, cfg.camera_id, cfg.site_id, vnow,
                    zone_id=gone_zone,
                    duration=round(vnow - restricted_since.pop(tid, vnow), 2),
                    **gone_who))
            if gone_zone and (pr, gone_zone) in loiter_started:
                pending_events.append(new_event(
                    LOITERING_END, cfg.camera_id, cfg.site_id, vnow,
                    zone_id=gone_zone, dwell_time=dwell.dwell(tid, vnow),
                    **gone_who))
                loiter_started.discard((pr, gone_zone))
            # A track that vanished before its full sample set still yields a
            # row if it had at least one scored crop; the crop for it was cut
            # when it was scored, so the frame here is not needed.
            _tg = attrs.drop(tid, vnow)
            if _tg is not None:
                _emit_attribute_tag(_tg, prev_zone.get(tid))
            deb.drop(tid)
            dwell.drop(tid)
            reid.drop(tid)          # frees the feature bank + releases the binding
            eng.drop_person(pr)
            prev_zone.pop(tid, None)
            restricted_since.pop(tid, None)
            # PPE state follows the tracker's lifecycle, no separate identity.
            # Without this the state dict grows without bound on a busy site,
            # and a recycled ByteTrack id inherits a stranger's verdict.
            ppe_tracker.drop_track(tid)
            ppe_last_seen.pop(tid, None)
            done = registry.complete(tid)          # final per-track summary
            if done is not None:
                completed_tracks += 1
                log.debug("track %s completed: %s", tid, done.summary())
        if stale:
            log.debug("evicted %d stale track(s); active=%d", len(stale),
                      reaper.active_count())

        # Stamp movement events with the occupancy they resulted in.
        #
        # Deliberately here and not at the point each event is built: the
        # occupancy Counter is filled DURING the track loop, so a ZONE_ENTRY
        # created halfway through would read a count that is still missing every
        # track after it. By this line the loop and the reaper have both
        # finished and the count is final for this frame.
        _stamp_zone_occupancy(pending_events, occupancy, cfg.zones)

        # 5s cadence: heartbeat (local record), density events, zone-state, rules
        heartbeat_event = None
        if agg.due(vnow):
            # Claim the tick. Without this the aggregator never learns an
            # emission happened, due() stays True on every frame, and this whole
            # block — density events, zone-state posts, heartbeats and rule
            # evaluation — runs at the processing rate instead of every 5s.
            agg.mark(vnow)
            # PPE counts are rebuilt HERE, from the same registry snapshot the
            # occupancy below is computed from — not carried over from the last
            # 2 Hz detector tick.
            #
            # Caching them across ticks broke the invariant this payload
            # promises: the counts described who was in the zone up to 5s ago
            # while `occupancy` described who is in it now, and the two
            # disagreed whenever anyone moved. Observed as 6+2+0=8 against an
            # occupancy of 7. The VERDICTS are persistent per track and can be
            # stale without harm; the MEMBERSHIP cannot.
            ppe_zone_counts = {}
            if _ppe_zones:
                # Built from THIS FRAME's tracks and zone_by_tid — the exact
                # pair `occupancy` above is derived from (see the `confirmed`
                # branch in pass 1), so the two describe the same people.
                #
                # My first two attempts at this both broke the invariant the
                # payload promises. Caching the counts from the 2 Hz detector
                # tick made them describe a moment up to 5s earlier. Rebuilding
                # from registry.active() looked right but was still wrong: the
                # registry keeps departed tracks alive for track_ttl_seconds,
                # so it counted people the frame no longer contains — observed
                # as 7+5+1=13 against an occupancy of 8. Only the frame's own
                # tracks agree with the frame's own occupancy.
                ppe_zone_counts = ppe_tracker.zone_summary([
                    (tid, zone_by_tid.get(tid),
                     ppe_tracker.assessable((x1, y1, x2, y2)))
                    for (tid, x1, y1, x2, y2) in tracks
                    if zone_by_tid.get(tid) in _ppe_zones])
            eng.camera.heartbeat(cfg.camera_id, vnow)
            heartbeat_event = new_event(CAMERA_HEARTBEAT, cfg.camera_id, cfg.site_id, vnow)
            for z in cfg.zones:
                occ = occupancy[z.zone_id]
                dens = density_per_sqm(occ, z.area_sqm)
                cap_pct = capacity_pct(occ, z.capacity_max)
                zstats.record(z.zone_id, occ, vnow)
                zstatus = density_status(dens, z.warning_density, z.critical_density)
                # Only on a NORMAL/WARNING/CRITICAL crossing by default — this
                # event was 99.85% of the events table, duplicating the
                # zone_state_ts row written on the next line at the same
                # microsecond. See finblade/emission.py.
                if density_gate.should_emit(z.zone_id, zstatus):
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
                    "status": zstatus,
                    # Identities, so the API can count distinct people across
                    # the cameras that share a physical area. Sorted for a
                    # stable payload; occupancy above stays the camera's own
                    # observation and is unchanged.
                    "occupants": sorted(zone_occupants.get(z.zone_id, ())),
                    "physical_area_id": z.physical_area_id,
                    "ts": vnow, **roll,
                    # PPE compliance as STATE, not as an event. The alert feed
                    # records transitions and structurally cannot answer "how
                    # many with hardhats, how many without" - one worker
                    # missing two items is two alerts and a compliant worker is
                    # none. These counts are PEOPLE and satisfy
                    #   compliant + non_compliant + not_assessable == occupancy
                    # while `violations` is per ITEM and will sum higher.
                    # Absent entirely on zones with no required_ppe.
                    **(ppe_state_fields(z.zone_id, occ, _ppe_zones,
                                        ppe_zone_counts, cfg.camera_id)
                       if z.zone_id in _ppe_zones else {}),
                })
                # Head-count threshold (REQ-21). Independent of area and
                # capacity, so it works in a small space that has neither
                # measured accurately.
                if z.zone_id in occ_threshold:
                    oa = eng.evaluate_occupancy(z.zone_id, occ,
                                                occ_threshold[z.zone_id], vnow)
                    if oa:
                        pending_alerts.append(oa.as_dict())
                for al in eng.evaluate_zone(z.zone_id, dens, cap_pct, vnow,
                                            warning_on=z.warning_density,
                                            critical_on=z.critical_density):
                    pending_alerts.append(al.as_dict())
                    if al.rule_id == "R-03" and al.kind == "FIRE":
                        pending_events.append(new_event(
                            CAPACITY_WARNING, cfg.camera_id, cfg.site_id, vnow,
                            zone_id=z.zone_id, occupancy=occ, capacity_pct=cap_pct))

        # --- PPE compliance (R-11) -----------------------------------------
        # Per TRACK, inside a compliance ZONE. Not a camera-level verdict: a
        # violation belongs to a person, and an alert that cannot say who is
        # not actionable.
        if _ppe_zones and any(d.due(vnow) for d in _ppe_detectors):
            ppe_roll = []
            # EVERY enabled detector runs on this tick, not just the one that
            # tripped the cadence. Letting them run on separate frames would
            # make each one's items read as "absent" on the frames the other
            # owned, and absence is evidence toward a violation — the models
            # would slowly convict each other's people.
            _dets = []
            for _d in _ppe_detectors:
                _dets.extend(_d.detect(frame, vnow, frame_id=seq))
            # Only people currently standing in a PPE zone are judged.
            _people = {}
            for t in registry.active():
                zid = t.current_zone_id
                if zid in _ppe_zones:
                    _people[t.track_id] = tuple(t.bbox)
            # Associate every PPE detection to at most one of them. Items that
            # cannot be attributed confidently are DROPPED, not guessed - see
            # geometry.associate_item.
            _items = [(d["bbox"], d["class_name"].replace("no_", ""))
                      for d in _dets if d["class_name"] != "person"]
            _owned = {}          # track_id -> [(class_name, conf), ...]
            for idx, owner, _score in associate_items(_items, _people):
                if owner is None:
                    continue
                d = [x for x in _dets if x["class_name"] != "person"][idx]
                _owned.setdefault(owner, []).append(
                    (d["class_name"], d["confidence"]))

            ppe_roll = []        # (track, zone, assessable) for the zone counts
            for _tid, _pbox in _people.items():
                _t = registry.get(_tid)
                _zid = _t.current_zone_id
                ppe_tracker.note_in_zone(_tid, _zid, vnow)
                # THE HEIGHT GATE. Too small to judge means NOT JUDGED - not
                # judged as absent, which would drift them into a violation on
                # the detector's blindness rather than their behaviour.
                _ok = ppe_tracker.assessable(_pbox)
                ppe_roll.append((_tid, _zid, _ok))
                if not _ok or ppe_tracker.in_grace(_tid, _zid, vnow):
                    continue
                _dt = vnow - ppe_last_seen.get(
                    _tid, vnow - (_ppe_detectors[0].interval_s
                                  if _ppe_detectors else 0.5))
                ppe_last_seen[_tid] = vnow
                for _req in _ppe_zones[_zid]:
                    _ev, _conf = ppe_evidence_for(_req, _owned.get(_tid, []))
                    _new = ppe_tracker.observe(_tid, _req, _ev, _conf, vnow, _dt)
                    if not _new:
                        continue
                    _st = ppe_tracker.state_of(_tid, _req)
                    _al = eng.evaluate_ppe(cfg.camera_id, _zid, _tid, _req,
                                           _new, _st, vnow,
                                           person_ref=_t.person_ref)
                    if _al is None:
                        continue
                    pending_alerts.append(_al.as_dict())
                    if _al.kind == "FIRE":
                        pending_events.append(new_event(
                            PPE_VIOLATION, cfg.camera_id, cfg.site_id, vnow,
                            zone_id=_zid, person_ref=_t.person_ref or "",
                            ppe_type=_req, violation_type="missing_" + _req,
                            first_seen=float(_st.first_seen or vnow),
                            confirmed_at=float(_st.confirmed_at or vnow),
                            track_id=int(_tid),
                            evidence=_st.summary(),
                            confidence=round(float(_conf), 4)))
                    else:
                        pending_events.append(new_event(
                            PPE_COMPLIANT, cfg.camera_id, cfg.site_id, vnow,
                            zone_id=_zid, person_ref=_t.person_ref or "",
                            ppe_type=_req, track_id=int(_tid),
                            evidence=_st.summary()))

        # --- fire / smoke (R-10) -------------------------------------------
        # Camera-scoped, not zone-scoped: a fire is a property of the picture,
        # and a camera with no polygons drawn must still be able to raise one.
        # zone_id stays None and the rule keys on the camera instead.
        #
        # Fed ONLY when the detector actually ran, but fed with a reading for
        # EVERY class including 0.0 — that zero is what lets the latch clear.
        # Skipping the call on a quiet tick would latch the alert on for ever.
        if hazard.due(vnow):
            for _cls, (_conf, _n) in hazard.observe(frame, vnow).items():
                _ha = eng.evaluate_hazard(None, _cls, _conf, vnow,
                                          camera_id=cfg.camera_id)
                if _ha is None:
                    continue
                pending_alerts.append(_ha.as_dict())
                if _ha.kind == "FIRE":
                    pending_events.append(new_event(
                        HAZARD_FIRE if _cls == "fire" else HAZARD_SMOKE,
                        cfg.camera_id, cfg.site_id, vnow,
                        confidence=round(float(_conf), 4), detections=int(_n)))

        # --- annotate once, then bookmark this moment if anything happened ---
        # per-track dwell + loiter flags for the feed overlay (Req 13/20)
        # PPE verdict per track for the overlay. Confirmed violations only —
        # the box must agree with the alert feed, so a track mid-timer is not
        # yet marked. ppe_judged False means the height gate excluded them.
        _ppe_meta = {}
        if _ppe_zones:
            _assessable = {tid: ok for tid, _z, ok in ppe_roll}
            for t in registry.active():
                if t.current_zone_id not in _ppe_zones:
                    continue
                if not _assessable.get(t.track_id, True):
                    _ppe_meta[t.track_id] = {"ppe_missing": None,
                                             "ppe_judged": False}
                    continue
                miss = [r for r in _ppe_zones[t.current_zone_id]
                        if ppe_tracker.verdict(t.track_id, r) == "NONCOMPLIANT"]
                _ppe_meta[t.track_id] = {"ppe_missing": miss, "ppe_judged": True}
        track_meta = {t.track_id: {
            "dwell": t.dwell_time,
            "loiter": (t.person_ref, t.current_zone_id) in loiter_started,
            "gref": reid.global_ref(t.track_id),   # None until ReID resolves it
            **_ppe_meta.get(t.track_id, {}),
        } for t in registry.active()}
        _hz_boxes = hazard.boxes_for(vnow)
        annotated = annotate(frame.copy(), cfg.zones, tracks, occupancy,
                             track_meta, hazards=_hz_boxes)
        okj, buf = cv2.imencode(".jpg", annotated)
        if okj:
            with _lock:
                _latest_jpeg["buf"] = buf.tobytes()
                # snapshot raw context for toggle-aware re-annotation in the stream
                _render.update(frame=frame, zones=cfg.zones, tracks=list(tracks),
                               occ=dict(occupancy), meta=track_meta,
                               hazards=_hz_boxes)

        # Snapshot only critical density + restricted intrusion — not loitering,
        # density-warning, capacity, or movement events.
        snap_alerts = [a for a in pending_alerts
                       if a.get("rule_id") in SNAPSHOT_RULES and a.get("kind") == "FIRE"]
        frame_ref = None
        # R-11 gets a CROP of the accused person, not the whole frame.
        #
        # A PPE violation names an individual, and reviewing it means looking at
        # that individual: on a 1920x1080 frame a worker is a couple of hundred
        # pixels tall and "is that a hardhat?" is genuinely hard to answer. A
        # crop makes the question answerable in a second, which matters most for
        # the alert type someone might reasonably dispute.
        #
        # Cropped from the ANNOTATED frame so the box and the "NO HAT" label
        # come with it, and padded so the person is not cut out of their own
        # context — a head-and-shoulders crop with no floor under it is hard to
        # place in the scene.
        ppe_boxes = {t[0]: t[1:] for t in tracks}
        for _a in snap_alerts:
            if _a.get("rule_id") != "R-11":
                continue
            _bx = ppe_boxes.get(_a.get("track_id"))
            if _bx is None:
                continue          # track gone this frame; fall back to the full frame
            _x1, _y1, _x2, _y2 = _bx
            _pw, _ph = (_x2 - _x1) * 0.35, (_y2 - _y1) * 0.12
            _cx1 = max(0, int(_x1 - _pw)); _cy1 = max(0, int(_y1 - _ph))
            _cx2 = min(annotated.shape[1], int(_x2 + _pw))
            _cy2 = min(annotated.shape[0], int(_y2 + _ph))
            if _cx2 - _cx1 < 16 or _cy2 - _cy1 < 16:
                continue
            bookmark_seq += 1
            _cname = f"bm_{cfg.camera_id}_{bookmark_seq:05d}_t{_a.get('track_id')}.jpg"
            cv2.imwrite(os.path.join(BOOKMARKS_DIR, _cname),
                        annotated[_cy1:_cy2, _cx1:_cx2])
            # Per-alert, so two workers violating at once get their own crops
            # rather than sharing one frame in which neither is obvious.
            _a["frame"] = "/bookmarks/" + _cname
        if any(a.get("rule_id") != "R-11" or not a.get("frame")
               for a in snap_alerts):
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
            # Do not overwrite a per-alert crop already attached above; the
            # shared full frame is the fallback for alerts that did not get one.
            if (frame_ref and not al.get("frame")
                    and al.get("rule_id") in SNAPSHOT_RULES
                    and al.get("kind") == "FIRE"):
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
        # Whether fire/smoke was actually being watched this run. "disabled"
        # and "unavailable: FileNotFoundError" must be distinguishable from
        # "ready, saw nothing" — otherwise a run with no fire alerts looks
        # identical whether the detector was working or was never loaded.
        "hazard": hazard.snapshot(),
        # Was anyone being tagged this run, and how often did the model decline
        # to say ("unknown")? A high unknown share is the number to watch: it
        # means the crops are too small or the lighting too poor to describe.
        "attributes": dict(attr_scorer.snapshot(), **attrs.snapshot(),
                           crops_saved=attr_counter[0]),
        # Same reasoning: "disabled" and "unavailable" must be distinguishable
        # from "ran and everybody was compliant". A run with no PPE alerts
        # looks identical either way without this.
        "ppe": dict(ppe.snapshot(), zones=_ppe_zones,
                    profiles=_ppe_profiles,
                    tracked_states=ppe_tracker.tracked()),
        # Reported separately and ALWAYS, even when disabled — the evidence file
        # must be able to answer "was medical PPE judged on this run?" and
        # "disabled" has to be distinguishable from "ran and found nothing".
        # item_status carries the evaluation/experimental marking with the run,
        # so a reader of metrics.json cannot mistake an experimental item's
        # output for a measured one.
        "medical_ppe": dict(
            ppe_med.snapshot(),
            item_status={item: ppe_status_of(item)
                         for zid, items in _ppe_zones.items()
                         if _ppe_profiles.get(zid) == "medical"
                         for item in items}),
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
                                       r["occ"], r["meta"], overlay=ov,
                                       hazards=r.get("hazards"))
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
