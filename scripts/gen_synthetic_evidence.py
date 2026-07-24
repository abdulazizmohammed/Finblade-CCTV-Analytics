"""Generate SYNTHETIC evidence artifacts (UC-57 event-injection tooling).

*** THIS IS NOT A DETECTION RUN. NO CAMERA, NO YOLO. ***
It drives the REAL finblade core (zones/metrics/events/rules) with a scripted
scenario so the human can (a) see the event/alert JSON schema populated and
(b) confirm the rule engine fires amber -> red -> intrusion -> offline correctly,
all WITHOUT weights/video (which are still missing — see BLOCKERS.md).

The annotated frames + contact sheet in the evidence protocol CANNOT be produced
until real detection runs (needs cv2 + weights). That gap is flagged in MORNING.md.

Writes: evidence/events.jsonl, evidence/alerts.jsonl, evidence/metrics.json,
        evidence/README.md
"""

import json
import os
import sys

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _REPO)

from finblade.events import (
    CAMERA_HEARTBEAT, CAMERA_OFFLINE, DENSITY_UPDATE, ZONE_ENTRY, new_event,
)
from finblade.identity import PersonRefHasher
from finblade.metrics import capacity_pct, density_per_sqm
from finblade.rules import RuleEngine
from finblade.zones import Zone

EVIDENCE = os.path.join(_REPO, "evidence")
LOBBY = Zone("ZONE-01", "Lobby", False, 40, 60.0, [])
BAY = Zone("ZONE-02", "Restricted Bay", True, 5, 12.0, [])
CAM, SITE = "CAM-A-01", "SITE-DXB-01"


def main():
    os.makedirs(EVIDENCE, exist_ok=True)
    hasher = PersonRefHasher(session_salt="synthetic-demo")
    eng = RuleEngine()
    events, alerts = [], []

    # Scenario timeline (seconds). Occupancy in the lobby ramps to critical,
    # then one person enters the restricted bay, then the camera goes silent.
    lobby_occ_by_t = {
        0: 10, 5: 40, 10: 130, 15: 260, 20: 130, 25: 40,  # ~0.17->4.3/m2 and back
    }
    for t in sorted(lobby_occ_by_t):
        occ = lobby_occ_by_t[t]
        dens = density_per_sqm(occ, LOBBY.area_sqm)
        events.append(new_event(DENSITY_UPDATE, CAM, SITE, float(t),
                                zone_id=LOBBY.zone_id, occupancy=occ, density=dens))
        for al in eng.evaluate_zone(LOBBY.zone_id, dens,
                                    capacity_pct(occ, LOBBY.capacity_max), float(t)):
            alerts.append(al.as_dict())
        eng.camera.heartbeat(CAM, float(t))
        events.append(new_event(CAMERA_HEARTBEAT, CAM, SITE, float(t)))

    # Restricted-zone intrusion at t=12 (immediate CRITICAL, R-06).
    pr = hasher.ref(42)
    events.append(new_event(ZONE_ENTRY, CAM, SITE, 12.0,
                            zone_to=BAY.zone_id, person_ref=pr, confidence=0.94))
    intr = eng.evaluate_intrusion(pr, BAY.zone_id, True, 12.0)
    if intr:
        alerts.append(intr.as_dict())

    # Camera goes silent after t=25; at t=57 (>30s) offline fires (R-07).
    off = eng.camera.check(CAM, 57.0)
    if off:
        alerts.append(off.as_dict())
        events.append(new_event(CAMERA_OFFLINE, CAM, SITE, 57.0, last_seen=25.0))

    with open(os.path.join(EVIDENCE, "events.jsonl"), "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
    with open(os.path.join(EVIDENCE, "alerts.jsonl"), "w") as f:
        for a in alerts:
            f.write(json.dumps(a) + "\n")

    fired = {}
    for a in alerts:
        fired[a["rule_id"]] = fired.get(a["rule_id"], 0) + 1
    metrics = {
        "SYNTHETIC": True,
        "note": "NOT a detection run. No camera/YOLO. Core-only scenario replay. "
                "Annotated frames + contact_sheet.jpg require real detection "
                "(cv2 + weights) - see BLOCKERS.md B-1.",
        "events_emitted": len(events),
        "events_by_type": _count(events, "event_type"),
        "alerts_fired": len(alerts),
        "alerts_by_rule": fired,
        "detection_ran": False,
        "avg_detections_per_frame": None,
    }
    with open(os.path.join(EVIDENCE, "metrics.json"), "w") as f:
        json.dump(metrics, f, indent=2)

    _write_readme(metrics)
    print("[synthetic] wrote evidence:", metrics["events_emitted"], "events,",
          metrics["alerts_fired"], "alerts. Rules fired:", fired)


def _count(items, key):
    out = {}
    for it in items:
        out[it[key]] = out.get(it[key], 0) + 1
    return out


def _write_readme(metrics):
    with open(os.path.join(EVIDENCE, "README.md"), "w") as f:
        f.write(
            "# evidence/ — what is and isn't here\n\n"
            "## Present (SYNTHETIC — core replay, NOT detection)\n"
            "- `events.jsonl` / `alerts.jsonl` — real schema, produced by the real\n"
            "  rule engine over a scripted scenario. Proves amber->red->intrusion->\n"
            "  offline fire correctly and that every event validates.\n"
            "- `metrics.json` — scenario summary. `detection_ran: false`.\n\n"
            "## MISSING (blocked on real detection — BLOCKERS.md B-1)\n"
            "- `frames/frame_*.jpg` annotated frames\n"
            "- `contact_sheet.jpg` (the single most useful human artifact)\n"
            "- real avg detections/frame, FPS, track-ID stability\n\n"
            "## To produce the real bundle (morning)\n"
            "1. Place `models/yolov8n.pt`; install CPU stack (BLOCKERS.md B-1).\n"
            "2. `python services/inference/run_cpu.py --config config/cameras.dev.yaml "
            "--seconds 60 --no-serve`\n"
            "3. Open `evidence/contact_sheet.jpg` — check boxes are on people and\n"
            "   zone polygons sit on the floor.\n"
        )


if __name__ == "__main__":
    main()
