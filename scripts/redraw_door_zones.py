#!/usr/bin/env python3
"""Redraw the door zones on one camera into the three-zone layout the facility
roster needs: OUTSIDE (beyond the threshold) -> DOOR (a strip of floor across
the threshold) -> lobby (interior floor, adjacent, no gap).

    .venv/bin/python scripts/redraw_door_zones.py                 # dry run: prints what would be saved
    .venv/bin/python scripts/redraw_door_zones.py --apply         # saves through the API
    .venv/bin/python scripts/redraw_door_zones.py --apply --camera CAM-01 --lobby ZONE-02

Coordinates are normalised (0..1 of frame width/height) and were derived
from the operator's zone-editor screenshot of CAM-01 on 2026-09-17, so they
are within a percent or two of the floor — LOOK AT THEM IN THE ZONE EDITOR
AFTERWARDS and nudge a vertex if the threshold line is off. The previous
zone set is written to evidence/zones_<camera>_before.json first, so a bad
redraw is one POST away from undone.

Why this layout (finblade/presence.py, DoorPolicy):
  OUTSIDE -> DOOR -> LOBBY   = entry
  LOBBY   -> DOOR -> OUTSIDE = exit
  LOBBY   -> DOOR -> LOBBY   = turned back, no count
  OUTSIDE -> DOOR -> OUTSIDE = looked in, no count
Without an OUTSIDE zone everyone visible on the pavement through the glass
was "at the door" with no way to tell in from out (1,831 turned_back and
719 ambiguous crossings on the Wareed instance in three days).
"""
import argparse
import json
import os
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# CAM-01 (2688x1520), from the editor screenshot. Threshold = where the
# glass door meets the floor tiles, running down-left across the frame.
THRESHOLD = [(0.385, 0.405), (0.200, 0.560)]          # right corner, left corner
INWARD = (0.060, 0.070)                              # ~1.2 m into the lobby at that depth
GLASS_TOP = [(0.121, 0.193), (0.357, 0.057)]         # top-left, top-right of the door opening

LAYOUT = {
    "outside": {
        "zone_id": "ZONE-01-OUT", "zone_name": "Outside (pavement)", "zone_type": "OUTSIDE",
        "normalized_polygon": [GLASS_TOP[0], GLASS_TOP[1], THRESHOLD[0], THRESHOLD[1]],
    },
    "door": {
        "zone_id": "ZONE-01", "zone_name": "Door", "zone_type": "DOOR",
        "normalized_polygon": [THRESHOLD[0], THRESHOLD[1],
                               (THRESHOLD[1][0] + INWARD[0], THRESHOLD[1][1] + INWARD[1]),
                               (THRESHOLD[0][0] + INWARD[0], THRESHOLD[0][1] + INWARD[1])],
    },
}


def _env():
    p = os.path.join(REPO, ".env")
    if os.path.exists(p):
        for line in open(p):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k, v)


def main(argv=None) -> int:
    import requests
    _env()
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--camera", default="CAM-01")
    ap.add_argument("--lobby", default="ZONE-02", help="the interior zone adjacent to the door")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)
    base = (os.environ.get("FINBLADE_SELF_URL") or "http://127.0.0.1:8000").rstrip("/")
    key = os.environ.get("FINBLADE_API_KEY")
    h = {"Authorization": f"Bearer {key}"} if key else {}

    zones = [z for z in requests.get(f"{base}/api/v1/zones", headers=h, timeout=15).json()["zones"]
             if z.get("camera_id") == args.camera]
    if not zones:
        print(f"no zones on {args.camera}"); return 1
    before = os.path.join(REPO, "evidence", f"zones_{args.camera}_before.json")
    os.makedirs(os.path.dirname(before), exist_ok=True)
    with open(before, "w") as fh:
        json.dump({"camera_id": args.camera, "zones": zones}, fh, indent=1)
    print(f"current zone set saved to {before}  (undo: POST it back to /api/v1/zones)")

    by_id = {z["zone_id"]: z for z in zones}
    door_old = by_id.get(LAYOUT["door"]["zone_id"], {})
    lobby = by_id.get(args.lobby)
    if lobby is None:
        print(f"lobby zone {args.lobby} not found on {args.camera}: {sorted(by_id)}"); return 1

    # Lobby: the door strip's INNER edge replaces whatever the lobby polygon
    # did near the threshold; the rest of its outline (wall, reception, bottom
    # of frame) is kept as drawn. Everything else on the zone stays.
    inner_right = LAYOUT["door"]["normalized_polygon"][3]
    inner_left = LAYOUT["door"]["normalized_polygon"][2]
    old = [tuple(p) for p in (lobby.get("normalized_polygon") or [])]
    # keep the vertices that are clearly away from the door (right/lower part)
    keep = [p for p in old if p[0] > 0.45 or p[1] > 0.75]
    new_lobby_poly = [inner_right] + keep + [inner_left]

    def _base(z, **over):
        # None values dropped: the store casts capacity/area with int()/float()
        out = {k: v for k, v in z.items() if v is not None and k not in ("polygon", "updated_at")}
        out.update(over)
        return out

    new_zones = [
        _base(door_old, **LAYOUT["door"], enabled=True,
              area_sqm=door_old.get("area_sqm") or 4.0),
        {**LAYOUT["outside"], "camera_id": args.camera, "enabled": True, "area_sqm": 0.0,
         "restricted": False, "required_ppe": []},
        _base(lobby, normalized_polygon=[list(p) for p in new_lobby_poly]),
    ] + [z for zid, z in by_id.items() if zid not in (LAYOUT["door"]["zone_id"], args.lobby, LAYOUT["outside"]["zone_id"])]
    for z in new_zones:
        z["camera_id"] = args.camera
        z["normalized_polygon"] = [[round(float(x), 4), round(float(y), 4)] for x, y in z["normalized_polygon"]]

    for z in new_zones:
        print(f"  {z['zone_id']:12s} {z['zone_type']:10s} {json.dumps(z['normalized_polygon'])}")
    if not args.apply:
        print("dry run — add --apply to save"); return 0
    r = requests.post(f"{base}/api/v1/zones", headers=h, timeout=15,
                      json={"camera_id": args.camera, "zones": new_zones})
    print(r.status_code, r.text[:300])
    if r.status_code == 200:
        print(f"saved. Open the zone editor on {args.camera} and check the threshold line.\n"
              f"undo: curl -X POST {base}/api/v1/zones -H 'Authorization: Bearer <full key>' "
              f"-H 'Content-Type: application/json' -d @{before}")
    return 0 if r.status_code == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
