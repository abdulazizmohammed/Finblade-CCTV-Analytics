#!/usr/bin/env python3
"""Generate FinBlade areas, door zones and camera topology from WiseNET BIM data.

WHY THIS EXISTS. Two cameras watching one room report that room twice. FinBlade
already knows how to fix that — map both cameras' zones to one PhysicalArea and
occupancy becomes the union of identities rather than the sum of counts (see
finblade/areas.py) — but the mapping is deliberately EXPLICIT and never inferred
from zone names, so somebody has to state it. On a real site that is a survey.
On WiseNET it is already written down, and this script transcribes it.

WHAT THE DATASET STATES, AND WHAT THIS SCRIPT ONLY INFERS
---------------------------------------------------------
Stated outright in network_enviroment/camera_calibration/<res>/cam_N.json:

    "deviceID":   "SmartCamera_2",
    "isHostedBy": "IfcSpace_102572",          <- the room the camera is IN
    "regionsOfInterest": [
        {"xywh": [287, 147, 24, 112], "represents": "IfcDoor_588559"}
    ]                                          <- where each door appears in frame

That gives, with no guesswork at all:

  * which cameras share a room  -> PhysicalArea mapping + topology overlapping_pairs
  * where the doors are in each frame -> DOOR zones for the presence roster

INFERRED (and flagged as such in the output):

  * space adjacency. Two spaces are treated as adjacent when a door appears in
    the calibration of cameras hosted by both. That is a sound reading of the
    data but it is not a statement the dataset makes.
  * transit times. NOT derivable. gt.json's tracklets are contiguous — one
    space's tracklet ends at the exact timestamp the next begins — so the
    annotation contains no walk duration to measure. Adjacent pairs therefore
    get min_seconds: 0.0, which is what config/topology.yaml already argues for
    on unsurveyed pairs, and non-adjacent pairs are emitted as comments for a
    human rather than invented.

THE VIDEO -> CAMERA MAPPING. videoN_M.avi is SmartCamera_M, uniformly across all
11 sets. That is not assumed from the filename: the per-frame "deviceID" in
manual_annotations/people_detection/set_N/videoN_M.json says so, and --verify
re-checks every set file against the filename index and reports any mismatch.
Files with an empty deviceID are videos in which nobody appears; they carry no
contradiction, so they are counted separately rather than treated as failures.

ZONE ORDER MATTERS — DO NOT SORT THE OUTPUT. finblade.zones.zone_of() returns
the FIRST zone whose polygon contains the foot point, after a stable sort that
only lifts restricted zones to the front. Neither the room zone nor a door zone
is restricted, so between them input order decides. Doors are therefore emitted
BEFORE the room zone: a person standing in a doorway must resolve to the DOOR
zone, or presence.py sees them move room -> room and never records a crossing.

WHY --door-zones DEFAULTS TO none. A door region says where the door APERTURE
appears in that camera's picture. It does NOT say where the floor a person walks
across is, and zone assignment uses the foot point. On set 1 the two regions on
camera 1 cover 42% of the frame between them, because the doors are close and
fill most of the picture. Turned into crossing zones, nearly half that room's
floor becomes "in a doorway" permanently.

Worse, there are two defensible readings of the same rectangle and they are
opposites:

    --door-zones door   the region is floor you cross -> DOOR zones, feeding
                        the presence roster's entry/exit model
    --door-zones mask   the region is a view THROUGH the doorway into the next
                        room -> UNMONITORED zones, so people standing in the
                        room beyond are not counted in this one

Which one a given box is cannot be settled from the JSON; it needs somebody to
look at the frame. So the default emits neither, and the room mapping — the part
that fixes double counting and needs no visual judgement — lands on its own. The
script prints each camera's door coverage so the decision has a number behind it.

Reads the dataset read-only. Writes only the files you name, and --apply is
opt-in.

Usage
-----
    # inspect what it would do (default; writes nothing, applies nothing)
    python3 scripts/import_wisenet_topology.py \\
        --dataset /mnt/c/Users/ICSADMIN/Downloads/data/wisenet_dataset --set 1

    # write the config files
    python3 scripts/import_wisenet_topology.py --dataset <path> --set 1 \\
        --out-topology config/topology.wisenet.yaml \\
        --out-plan     config/areas.wisenet.json

    # ...and push areas + zones into a running API
    python3 scripts/import_wisenet_topology.py --dataset <path> --set 1 --apply

Then restart the API against the generated topology, because it is read once at
import:

    FINBLADE_TOPOLOGY=config/topology.wisenet.yaml \\
        .venv/bin/python -m uvicorn services.api.app:app --host 0.0.0.0 --port 8000
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections import defaultdict

# Sets 1-4 were recorded at 1280x720, 5-11 at 640x480. The calibration ships
# both, and the door pixel boxes differ between them, so picking the wrong one
# puts every door zone in the wrong place.
SETS_720 = {1, 2, 3, 4}

DEFAULT_CAMERA_FORMAT = "WN-S{set}-C{cam:02d}"
DEFAULT_SITE_ID = "SITE-WISENET"


# ---------------------------------------------------------------------------
# Reading the dataset
# ---------------------------------------------------------------------------

def resolution_for_set(set_no):
    return (1280, 720) if set_no in SETS_720 else (640, 480)


def _network_dir(dataset):
    """The BIM directory. The dataset ships it misspelled; accept both."""
    for name in ("network_enviroment", "network_environment"):
        path = os.path.join(dataset, name)
        if os.path.isdir(path):
            return path
    raise SystemExit(
        "no network_enviroment/ directory under %s — is that the dataset root?"
        % dataset)


def load_element_ids(dataset):
    """IFC id -> the short label used in topology.eps (s1..s6, d1..d7).

    Only for human-readable names; nothing downstream depends on it, so a
    missing file degrades to using the raw IFC ids.
    """
    path = os.path.join(_network_dir(dataset), "element_IDs.txt")
    ids = {}
    if not os.path.exists(path):
        return ids
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = re.match(r"^\s*(Ifc\w+_\d+)\s*;\s*(\w+)\s*$", line)
            if m:
                ids[m.group(1)] = m.group(2)
    return ids


def load_calibration(dataset, width, height):
    """cam number -> {device, space, rois}. This is the authoritative source."""
    path = os.path.join(_network_dir(dataset), "camera_calibration",
                        "%d_%d" % (width, height))
    if not os.path.isdir(path):
        raise SystemExit("no calibration for %dx%d at %s" % (width, height, path))

    cams = {}
    for fn in sorted(os.listdir(path)):
        m = re.match(r"^cam_(\d+)\.json$", fn)
        if not m:
            continue
        with open(os.path.join(path, fn), "r", encoding="utf-8") as fh:
            j = json.load(fh)
        rois = []
        for r in j.get("regionsOfInterest") or ():
            xywh = r.get("xywh")
            if not xywh or len(xywh) != 4:
                continue
            rois.append({
                "roi_id": r.get("regionOfInterest"),
                "xywh": [int(round(float(v))) for v in xywh],
                "door": r.get("represents"),
            })
        cams[int(m.group(1))] = {
            "device": j.get("deviceID"),
            "space": j.get("isHostedBy"),
            "rois": rois,
        }
    if not cams:
        raise SystemExit("no cam_*.json found in %s" % path)
    return cams


def videos_for_set(dataset, set_no):
    """cam number -> video filename, from the set's own .avi files.

    Falls back to the annotation directory when video_sets/ is not alongside
    (the videos are often copied into the app's media/ and the annotations left
    where they were).
    """
    candidates = [
        (os.path.join(dataset, "video_sets", "set_%d" % set_no), ".avi"),
        (os.path.join(dataset, "manual_annotations", "people_detection",
                      "set_%d" % set_no), ".json"),
    ]
    for path, ext in candidates:
        if not os.path.isdir(path):
            continue
        found = {}
        for fn in sorted(os.listdir(path)):
            m = re.match(r"^video%d_(\d+)\%s$" % (set_no, ext), fn)
            if m:
                found[int(m.group(1))] = fn
        if found:
            return found
    return {}


def verify_device_mapping(dataset, set_no):
    """Re-derive videoN_M -> SmartCamera_M from the annotations' own deviceID.

    The whole area mapping hangs on this index being the camera number, so it is
    checked rather than trusted. Returns (confirmed, silent, mismatches).
    """
    path = os.path.join(dataset, "manual_annotations", "people_detection",
                        "set_%d" % set_no)
    confirmed, silent, mismatches = [], [], []
    if not os.path.isdir(path):
        return confirmed, silent, mismatches

    for fn in sorted(os.listdir(path)):
        m = re.match(r"^video%d_(\d+)\.json$" % set_no, fn)
        if not m:
            continue                      # .bkp and other strays
        idx = int(m.group(1))
        try:
            with open(os.path.join(path, fn), "r", encoding="utf-8") as fh:
                doc = json.load(fh)
        except (ValueError, OSError) as exc:
            mismatches.append("%s: unreadable (%s)" % (fn, exc.__class__.__name__))
            continue
        devices = {fr.get("deviceID") for fr in (doc.get("frames") or ())
                   if fr.get("deviceID")}
        if not devices:
            # No detections in this video at all — nobody walked past. Carries
            # no evidence either way, so it is not a failure.
            silent.append((idx, fn))
            continue
        expected = {"SmartCamera_%d" % idx}
        if devices == expected:
            confirmed.append((idx, fn))
        else:
            mismatches.append("%s: filename implies %s, annotation says %s"
                              % (fn, sorted(expected), sorted(devices)))
    return confirmed, silent, mismatches


# ---------------------------------------------------------------------------
# Deriving the model
# ---------------------------------------------------------------------------

def build_model(cams, cameras_in_set, element_ids):
    """Group cameras by space and work out which spaces share a door."""
    space_cams = defaultdict(list)
    for num in sorted(cameras_in_set):
        cam = cams.get(num)
        if cam and cam.get("space"):
            space_cams[cam["space"]].append(num)

    # A door is "between" the spaces whose cameras can see it. With cameras on
    # both sides that gives the pair directly; with a camera on only one side it
    # gives a single space, which means a door to somewhere uncovered — an exit
    # from the monitored area, not an internal link.
    door_spaces = defaultdict(set)
    for num in sorted(cameras_in_set):
        cam = cams.get(num)
        if not cam or not cam.get("space"):
            continue
        for roi in cam["rois"]:
            if roi["door"]:
                door_spaces[roi["door"]].add(cam["space"])

    adjacency = defaultdict(set)
    boundary_doors = []
    for door, spaces in door_spaces.items():
        spaces = sorted(spaces)
        if len(spaces) >= 2:
            for i, a in enumerate(spaces):
                for b in spaces[i + 1:]:
                    adjacency[a].add(b)
                    adjacency[b].add(a)
        else:
            boundary_doors.append((door, spaces[0] if spaces else None))

    return {
        "space_cams": dict(space_cams),
        "door_spaces": {d: sorted(s) for d, s in door_spaces.items()},
        "adjacency": {a: sorted(b) for a, b in adjacency.items()},
        "boundary_doors": sorted(boundary_doors),
        "element_ids": element_ids,
    }


def hop_counts(adjacency, spaces):
    """Shortest number of doorways between every pair. Reported, never applied.

    Two spaces three doors apart plainly need a longer minimum transit than two
    that share a wall, but turning hops into seconds requires knowing how big
    the building is — which the calibration does not say. So this is printed to
    tell a human where surveying actually matters.
    """
    dist = {}
    for start in spaces:
        seen = {start: 0}
        queue = [start]
        while queue:
            node = queue.pop(0)
            for nxt in adjacency.get(node, ()):
                if nxt not in seen:
                    seen[nxt] = seen[node] + 1
                    queue.append(nxt)
        for other, d in seen.items():
            if start != other:
                dist[tuple(sorted((start, other)))] = d
    return dist


def space_label(space_id, element_ids):
    """'s1' where the dataset gives one, else the raw IFC id."""
    return element_ids.get(space_id) or space_id


def area_id_for(space_id, element_ids):
    """Stable across sets on purpose — set 1 and set 5 are the same building.

    Aligned with gt.json's own vocabulary ("space 2"), so a later evaluation can
    join area occupancy to ground truth without a second lookup table.
    """
    label = space_label(space_id, element_ids)
    m = re.match(r"^s(\d+)$", str(label))
    return "WN-SPACE-%s" % m.group(1) if m else "WN-%s" % label


def door_label(door_id, element_ids):
    return (element_ids.get(door_id) or door_id).upper()


# ---------------------------------------------------------------------------
# Emitting FinBlade config
# ---------------------------------------------------------------------------

def build_areas(model):
    """One PhysicalArea per space. This is the object that stops double counting."""
    element_ids = model["element_ids"]
    areas = []
    for space_id in sorted(model["space_cams"]):
        cams = model["space_cams"][space_id]
        label = space_label(space_id, element_ids)
        areas.append({
            "area_id": area_id_for(space_id, element_ids),
            "name": "WiseNET space %s" % str(label).lstrip("s"),
            "area_type": "ROOM",
            # Unknown from the calibration — the floor plan is in I3M.ifc and
            # parsing IFC geometry is a different job. 0 disables density and
            # capacity rules rather than feeding them a made-up number.
            "area_sqm": 0.0,
            "capacity_max": 0,
            "_ifc_space": space_id,
            "_cameras": cams,
        })
    # By area_id, not IFC id: the printed table is the thing a human checks
    # against the floor plan, and s1/s2/s3 ordering reads like the plan while
    # IfcSpace_102572 < IfcSpace_23922 does not.
    areas.sort(key=lambda a: a["area_id"])
    return areas


def build_zones(model, cams, cameras_in_set, camera_ids, width, height,
                door_mode="none"):
    """Per camera: optionally the doors it can see, then the room floor.

    ``door_mode`` picks what a door region becomes, and the choice is not
    cosmetic — see the module docstring. Order is load-bearing either way:
    zone_of() takes the FIRST polygon containing the foot point after a stable
    sort that only lifts restricted zones, so doors must precede the room zone
    or they can never win.
    """
    element_ids = model["element_ids"]
    out = {}
    for num in sorted(cameras_in_set):
        cam = cams.get(num)
        if not cam or not cam.get("space"):
            continue
        camera_id = camera_ids[num]
        area_id = area_id_for(cam["space"], element_ids)
        zones = []

        for roi in (cam["rois"] if door_mode != "none" else ()):
            x, y, w, h = roi["xywh"]
            dlabel = door_label(roi["door"], element_ids)
            if door_mode == "door":
                zone = {
                    "zone_id": "%s-DOOR-%s" % (camera_id, dlabel),
                    "zone_name": "Door %s" % dlabel,
                    # DOOR, not ENTRANCE/EXIT: these are internal doorways
                    # walked both ways, and presence.py resolves the direction
                    # from the zones either side of the crossing.
                    "zone_type": "DOOR",
                    # Mapped to the room the camera is in, so someone standing
                    # in the doorway still counts toward that room.
                    "physical_area_id": area_id,
                }
            else:                                   # "mask"
                zone = {
                    "zone_id": "%s-THRU-%s" % (camera_id, dlabel),
                    "zone_name": "Through door %s" % dlabel,
                    # UNMONITORED is a detection MASK: the foot point is
                    # discarded outright. Deliberately NOT mapped to an area —
                    # the point is that whoever is standing there belongs to the
                    # next room, not this one.
                    "zone_type": "UNMONITORED",
                    "physical_area_id": None,
                }
            zone.update({
                "polygon": [[x, y], [x + w, y], [x + w, y + h], [x, y + h]],
                "capacity_max": 0,
                "area_sqm": 0.0,
                "_ifc_door": roi["door"],
                "_roi_id": roi["roi_id"],
                "_frame_pct": round(100.0 * w * h / float(width * height), 1),
            })
            zones.append(zone)

        # -- then the room floor, covering the whole frame. The camera is INSIDE
        #    this space (isHostedBy), so everything it sees is that space. It
        #    also satisfies presence.py's requirement that the floor just inside
        #    a bidirectional door belong to an adjacent zone — without it a
        #    crossing reads as "door -> nowhere" and is discarded.
        zones.append({
            "zone_id": "%s-ROOM" % camera_id,
            "zone_name": "%s floor" % model_area_name(model, cam["space"]),
            "zone_type": "MONITORED",
            "physical_area_id": area_id,
            "polygon": [[0, 0], [width, 0], [width, height], [0, height]],
            "capacity_max": 0,
            "area_sqm": 0.0,
        })
        out[camera_id] = zones
    return out


def model_area_name(model, space_id):
    label = space_label(space_id, model["element_ids"])
    return "Space %s" % str(label).lstrip("s")


def build_topology_yaml(model, cams, cameras_in_set, camera_ids, set_no,
                        max_transit):
    """The topology file, hand-written so the reasoning travels with the values."""
    element_ids = model["element_ids"]
    L = []
    add = L.append

    add("# Camera topology for WiseNET set %d — GENERATED, do not hand-edit."
        % set_no)
    add("#")
    add("# Regenerate with:")
    add("#   python3 scripts/import_wisenet_topology.py --set %d \\" % set_no)
    add("#       --dataset <wisenet_dataset> --out-topology <this file>")
    add("#")
    add("# Source: network_enviroment/camera_calibration/*/cam_N.json.")
    add("# 'isHostedBy' states which room each camera is in; the door regions")
    add("# state which doorways it can see. Everything below follows from those")
    add("# two fields, except where a comment says otherwise.")
    add("")

    # -- overlapping pairs: the whole point of the exercise.
    add("# Cameras hosted by the SAME IfcSpace. They see one room between them,")
    add("# so the same person is legitimately on both at the same instant and a")
    add("# dt near zero is evidence FOR a match, not against it. Without these")
    add("# pairs declared, simultaneous sightings fall through the non-")
    add("# overlapping branch and every correct match is a coin toss on the")
    add("# default transit window.")
    add("overlapping_pairs:")
    pairs = []
    for space_id in sorted(model["space_cams"]):
        nums = [n for n in model["space_cams"][space_id] if n in cameras_in_set]
        label = space_label(space_id, element_ids)
        for i, a in enumerate(sorted(nums)):
            for b in sorted(nums)[i + 1:]:
                pairs.append((camera_ids[a], camera_ids[b], label, space_id))
    if pairs:
        for a, b, label, space_id in pairs:
            add("  # %s (%s)" % (label, space_id))
            add("  - a: %s" % a)
            add("    b: %s" % b)
    else:
        add("  []   # no space in this set is watched by more than one camera")
    add("")

    # -- transits between adjacent spaces.
    add("# Camera pairs in DIFFERENT rooms that share a doorway.")
    add("#")
    add("# min_seconds is 0.0 and that is a considered value, not a placeholder:")
    add("# the rooms share a door, so the walk between them really can be")
    add("# instantaneous, and topology.py argues at length that inventing a")
    add("# non-zero minimum for an unsurveyed pair rejects correct matches while")
    add("# looking healthy in the logs. gt.json cannot supply a better number —")
    add("# its tracklets are contiguous, so a space change takes zero annotated")
    add("# seconds. Measure the real walk if you want these tightened.")
    add("transits:")
    transit_lines = 0
    adjacency = model["adjacency"]
    for space_a in sorted(adjacency):
        for space_b in adjacency[space_a]:
            if space_b <= space_a:
                continue
            shared = sorted(d for d, s in model["door_spaces"].items()
                            if space_a in s and space_b in s)
            names = ", ".join(door_label(d, element_ids) for d in shared)
            for a in sorted(n for n in model["space_cams"].get(space_a, ())
                            if n in cameras_in_set):
                for b in sorted(n for n in model["space_cams"].get(space_b, ())
                                if n in cameras_in_set):
                    add("  # %s <-> %s via %s"
                        % (space_label(space_a, element_ids),
                           space_label(space_b, element_ids), names))
                    add("  - a: %s" % camera_ids[a])
                    add("    b: %s" % camera_ids[b])
                    add("    min_seconds: 0.0")
                    add("    max_seconds: %.1f" % max_transit)
                    transit_lines += 1
    if not transit_lines:
        add("  []")
    add("")

    # -- non-adjacent pairs are left for a human, with the hop count as a hint.
    spaces = sorted(model["space_cams"])
    dist = hop_counts(adjacency, spaces)
    far = [(p, d) for p, d in sorted(dist.items()) if d >= 2]
    unreachable = [tuple(sorted((a, b)))
                   for i, a in enumerate(spaces) for b in spaces[i + 1:]
                   if tuple(sorted((a, b))) not in dist]
    if far or unreachable:
        add("# NEEDS A HUMAN — pairs this script will not guess.")
        add("#")
        add("# These rooms do not share a door, so a person must cross at least")
        add("# one other space to get between them and the minimum transit is")
        add("# genuinely non-zero. How non-zero depends on the size of the")
        add("# building, which the calibration does not record. Pace the walk and")
        add("# add the pair to `transits` above.")
        for (a, b), d in far:
            add("#   %s <-> %s : %d doorways apart"
                % (space_label(a, element_ids), space_label(b, element_ids), d))
        for a, b in unreachable:
            add("#   %s <-> %s : no route through the monitored doors"
                % (space_label(a, element_ids), space_label(b, element_ids)))
        add("")

    add("default_transit:")
    add("  min_seconds: 0.0")
    add("  max_seconds: %.1f" % max_transit)
    add("")
    add("overlap_tolerance_seconds: 5.0")
    add("")
    add("# Every camera pair in this set is listed above, so an unlisted pair")
    add("# means a camera the topology does not know about. Left permissive")
    add("# because a stray camera should degrade matching, not stop it; set")
    add("# false once you are confident the set is fully described.")
    add("allow_unknown_pairs: true")
    add("")
    add("# Carried over from config/topology.yaml unchanged. These are ReID")
    add("# matching parameters, independent of the building, and WiseNET is the")
    add("# footage to retune them on — see the threshold comment there.")
    add("matching:")
    add("  threshold: 0.70")
    add("  margin: 0.06")
    add("  ttl_seconds: 300")
    add("  bank_capacity: 5")
    add("  max_identities: 2000")
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------------------
# Applying to a running API
# ---------------------------------------------------------------------------

def _strip_private(obj):
    """Drop the _-prefixed provenance keys before anything is POSTed."""
    if isinstance(obj, dict):
        return {k: _strip_private(v) for k, v in obj.items()
                if not str(k).startswith("_")}
    if isinstance(obj, list):
        return [_strip_private(v) for v in obj]
    return obj


def post_json(api_url, path, payload, api_key=None, timeout=10):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(api_url.rstrip("/") + path, data=body,
                                 method="POST")
    req.add_header("Content-Type", "application/json")
    if api_key:
        req.add_header("Authorization", "Bearer %s" % api_key)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8") or "{}")


def apply_to_api(areas, zones_by_camera, api_url, api_key):
    """POST areas then zones. Areas first — a zone naming an undefined area
    would create a bare one with no name or type."""
    failures = []
    for area in areas:
        try:
            code, body = post_json(api_url, "/api/v1/areas",
                                   _strip_private(area), api_key)
            print("  area %-14s -> %s" % (area["area_id"], code))
            if code != 200:
                failures.append("area %s: %s" % (area["area_id"], body))
        except (urllib.error.URLError, OSError) as exc:
            failures.append("area %s: %s" % (area["area_id"], exc))
            print("  area %-14s -> FAILED (%s)" % (area["area_id"], exc))

    for camera_id, zones in sorted(zones_by_camera.items()):
        payload = {"camera_id": camera_id, "zones": _strip_private(zones)}
        try:
            code, body = post_json(api_url, "/api/v1/zones", payload, api_key)
            print("  zones %-14s -> %s (%d zones)" % (camera_id, code, len(zones)))
            if code != 200:
                failures.append("zones %s: %s" % (camera_id, body))
        except (urllib.error.URLError, OSError) as exc:
            failures.append("zones %s: %s" % (camera_id, exc))
            print("  zones %-14s -> FAILED (%s)" % (camera_id, exc))
    return failures


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def find_rtsp_urls(repo_root, set_no):
    """RTSP URL per camera number, if tools/wisenet_rtsp has been scanned."""
    path = os.path.join(repo_root, "tools", "wisenet_rtsp", "wisenet_streams.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (ValueError, OSError):
        return {}
    urls = {}
    for name, entry in (doc.get("set_%d" % set_no) or {}).items():
        m = re.match(r"^cam_(\d+)$", name)
        if m and isinstance(entry, dict):
            urls[int(m.group(1))] = entry.get("rtsp") or entry.get("rtsp_localhost")
    return urls


def main(argv=None):
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

    ap = argparse.ArgumentParser(
        description="Generate FinBlade areas, door zones and topology from "
                    "WiseNET BIM data.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", required=True,
                    help="wisenet_dataset root (the directory holding "
                         "network_enviroment/ and manual_annotations/)")
    ap.add_argument("--set", type=int, required=True, dest="set_no",
                    help="video set 1-11")
    ap.add_argument("--camera-id-format", default=DEFAULT_CAMERA_FORMAT,
                    help="how camera ids are built; {set} and {cam} are "
                         "substituted. MUST match the ids you register the "
                         "cameras under. default: %(default)s")
    ap.add_argument("--site-id", default=DEFAULT_SITE_ID)
    ap.add_argument("--max-transit", type=float, default=120.0,
                    help="max_seconds for every emitted transit window "
                         "(default: %(default)s)")
    ap.add_argument("--door-zones", choices=("none", "door", "mask"),
                    default="none",
                    help="what to do with each camera's door regions. "
                         "'none' (default) emits only the room zone and is the "
                         "de-duplication fix on its own. 'door' emits DOOR "
                         "crossing zones for the presence roster. 'mask' emits "
                         "UNMONITORED zones that discard anyone seen through a "
                         "doorway. 'door' and 'mask' are opposites and BOTH "
                         "need a human to look at the boxes first")
    ap.add_argument("--out-topology", help="write the topology YAML here")
    ap.add_argument("--out-plan",
                    help="write areas + zones as JSON here (also the record of "
                         "what --apply would send)")
    ap.add_argument("--apply", action="store_true",
                    help="POST areas and zones to a running API. REPLACES the "
                         "existing zone set of every camera named.")
    ap.add_argument("--api-url", default="http://127.0.0.1:8000")
    ap.add_argument("--api-key", default=os.environ.get("FINBLADE_API_KEY"))
    ap.add_argument("--no-verify", action="store_true",
                    help="skip re-checking videoN_M -> SmartCamera_M against "
                         "the annotations")
    args = ap.parse_args(argv)

    if not 1 <= args.set_no <= 11:
        ap.error("--set must be 1-11")
    if not os.path.isdir(args.dataset):
        ap.error("--dataset %s is not a directory" % args.dataset)

    width, height = resolution_for_set(args.set_no)
    element_ids = load_element_ids(args.dataset)
    cams = load_calibration(args.dataset, width, height)

    videos = videos_for_set(args.dataset, args.set_no)
    cameras_in_set = sorted(videos) if videos else sorted(cams)
    # A set can contain a video for a camera the calibration does not describe.
    # Counting it would mean an area mapping we cannot justify, so it is dropped
    # loudly rather than guessed at.
    uncalibrated = [n for n in cameras_in_set if n not in cams]
    cameras_in_set = [n for n in cameras_in_set if n in cams]

    camera_ids = {n: args.camera_id_format.format(set=args.set_no, cam=n)
                  for n in cameras_in_set}

    model = build_model(cams, cameras_in_set, element_ids)
    areas = build_areas(model)
    zones_by_camera = build_zones(model, cams, cameras_in_set, camera_ids,
                                  width, height, args.door_zones)
    topology_yaml = build_topology_yaml(model, cams, cameras_in_set, camera_ids,
                                        args.set_no, args.max_transit)
    rtsp = find_rtsp_urls(repo_root, args.set_no)

    # ---- report -----------------------------------------------------------
    print("WiseNET set %d - %dx%d, %d camera(s)"
          % (args.set_no, width, height, len(cameras_in_set)))
    if uncalibrated:
        print("  ! %d video(s) have no calibration entry and were skipped: %s"
              % (len(uncalibrated), ", ".join(str(n) for n in uncalibrated)))
    print()

    if not args.no_verify:
        confirmed, silent, mismatches = verify_device_mapping(args.dataset,
                                                              args.set_no)
        if mismatches:
            print("VIDEO -> CAMERA MAPPING FAILED VERIFICATION:")
            for line in mismatches:
                print("  %s" % line)
            print("\nEvery area mapping below depends on this. Stopping.")
            return 2
        if confirmed or silent:
            print("video -> camera mapping verified: %d confirmed by annotation "
                  "deviceID, %d video(s) with no detections to check"
                  % (len(confirmed), len(silent)))
        else:
            print("video -> camera mapping NOT verified: no annotations found "
                  "under manual_annotations/people_detection/set_%d"
                  % args.set_no)
        print()

    print("Rooms and the cameras in them:")
    for area in areas:
        nums = area["_cameras"]
        ids = ", ".join(camera_ids[n] for n in nums if n in camera_ids)
        flag = "  <-- shared, this is where double counting came from" \
            if len(nums) > 1 else ""
        print("  %-14s %-22s %s%s"
              % (area["area_id"], "(%s)" % area["_ifc_space"], ids, flag))
    print()

    print("Zones per camera (--door-zones %s; earlier zones win ties):"
          % args.door_zones)
    for camera_id in sorted(zones_by_camera):
        for z in zones_by_camera[camera_id]:
            poly = z["polygon"]
            box = "x %d-%d  y %d-%d" % (poly[0][0], poly[1][0],
                                        poly[0][1], poly[2][1])
            pct = z.get("_frame_pct")
            print("  %-14s %-8s %-12s %-22s %-7s -> %s"
                  % (camera_id,
                     z["zone_id"].rsplit("-", 1)[-1] if pct is not None else "ROOM",
                     z["zone_type"], box,
                     "%.1f%%" % pct if pct is not None else "100%",
                     z["physical_area_id"] or "-"))
    print()

    if rtsp:
        print("Register these cameras (ids MUST match the mapping above):")
        for n in cameras_in_set:
            print("  %-14s %s" % (camera_ids[n], rtsp.get(n, "(no stream)")))
        print()

    # The door regions are the one part of this that a human has to rule on, so
    # report the number that decides it rather than a general caution. A door
    # ROI is where the door APERTURE appears in frame — on a close door that is
    # most of the picture, and treating it as a crossing zone would put half the
    # room's floor permanently "in a doorway".
    coverage = []
    for camera_id in sorted(zones_by_camera):
        pct = sum(z.get("_frame_pct") or 0.0 for z in zones_by_camera[camera_id])
        if pct:
            coverage.append((pct, camera_id))
    if args.door_zones == "none":
        door_pct = {}
        for num in cameras_in_set:
            total = sum(100.0 * r["xywh"][2] * r["xywh"][3] / float(width * height)
                        for r in cams[num]["rois"])
            if total:
                door_pct[camera_ids[num]] = total
        if door_pct:
            print("Door regions exist but were NOT emitted (--door-zones none).")
            print("  How much of each frame they cover:")
            for cid, pct in sorted(door_pct.items(), key=lambda kv: -kv[1]):
                warn = "   <-- too big to treat as a doorway" if pct > 25 else ""
                print("    %-14s %5.1f%%%s" % (cid, pct, warn))
            print("  This is why the default is 'none'. A door ROI marks where")
            print("  the door APERTURE appears, not the floor a person crosses,")
            print("  and zone assignment uses the foot point. Look at the boxes")
            print("  in the zone editor before choosing --door-zones door (they")
            print("  become crossing zones for the presence roster) or mask")
            print("  (they discard anyone seen through the doorway into the")
            print("  next room). Those are opposites; only you can see which")
            print("  each box actually is.")
            print()
    elif coverage:
        print("NEEDS YOUR EYES - I cannot see whether these boxes are on doors.")
        print("  Emitted as %s zones, covering this share of each frame:"
              % ("DOOR crossing" if args.door_zones == "door"
                 else "UNMONITORED mask"))
        for pct, cid in sorted(coverage, reverse=True):
            warn = "   <-- CHECK THIS ONE" if pct > 25 else ""
            print("    %-14s %5.1f%%%s" % (cid, pct, warn))
        print("  Anything above ~25%% is unlikely to be a doorway-sized region")
        print("  and will swallow ordinary floor. Open the zone editor on each")
        print("  camera before trusting entry/exit counts.")
        print()

    # ---- outputs ----------------------------------------------------------
    wrote = False
    if args.out_topology:
        path = os.path.join(repo_root, args.out_topology) \
            if not os.path.isabs(args.out_topology) else args.out_topology
        # newline="\n" because this file is read on the Linux box that runs the
        # API, and generating it from a Windows checkout would otherwise write
        # CRLF and show the whole file as modified on every regeneration.
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(topology_yaml)
        print("wrote %s" % path)
        wrote = True

    if args.out_plan:
        path = os.path.join(repo_root, args.out_plan) \
            if not os.path.isabs(args.out_plan) else args.out_plan
        plan = {
            "set": args.set_no,
            "site_id": args.site_id,
            "resolution": {"width": width, "height": height},
            "cameras": [{"camera_id": camera_ids[n], "camera_number": n,
                         "video": videos.get(n), "rtsp": rtsp.get(n),
                         "ifc_space": cams[n]["space"]}
                        for n in cameras_in_set],
            "areas": areas,
            "zones": zones_by_camera,
            "adjacency": model["adjacency"],
            "door_spaces": model["door_spaces"],
        }
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(plan, fh, indent=2, sort_keys=False)
            fh.write("\n")
        print("wrote %s" % path)
        wrote = True

    if args.apply:
        print("\napplying to %s ..." % args.api_url)
        failures = apply_to_api(areas, zones_by_camera, args.api_url, args.api_key)
        if failures:
            print("\n%d call(s) failed:" % len(failures))
            for f in failures:
                print("  %s" % f)
            return 1
        print("\napplied. The API picks the zone->area map up within 5s; the")
        print("topology file is read ONCE at import, so restart the API with")
        print("FINBLADE_TOPOLOGY set for the overlapping_pairs to take effect.")
    elif not wrote:
        print("(dry run - nothing written or applied; pass --out-topology, "
              "--out-plan or --apply)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
