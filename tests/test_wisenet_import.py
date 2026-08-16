"""The WiseNET BIM importer: rooms, zone mapping and topology.

These tests use synthetic calibration dicts rather than the dataset, so they run
on a machine that does not have it. The shapes mirror the real files exactly
(see network_enviroment/camera_calibration/1280_720/cam_N.json):

    cam 1, cam 2  -> IfcSpace_A   (one room, two cameras — the case that matters)
    cam 3         -> IfcSpace_B   (adjacent, shares door D_1 with the first room)
    cam 4         -> IfcSpace_C   (no shared door — two doorways away)

What is actually being pinned down here is that a room watched by two cameras
produces ONE area with both cameras mapped to it, and an overlapping_pairs entry
so simultaneous sightings are treated as evidence for a match rather than as
physically impossible. That is the whole point of the importer; everything else
is presentation.
"""

import importlib.util
import json
import os

import pytest

from finblade.areas import AreaOccupancy, AreaRegistry, area_from_dict, area_ref
from finblade.topology import CameraTopology
from finblade.zones import zone_from_dict, zone_of
from services.api.schema import validate_zones

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _load_importer():
    """Load scripts/import_wisenet_topology.py as a module (it is not a package)."""
    path = os.path.join(_REPO, "scripts", "import_wisenet_topology.py")
    spec = importlib.util.spec_from_file_location("wisenet_import", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


wn = _load_importer()

WIDTH, HEIGHT = 1280, 720

ELEMENT_IDS = {
    "IfcSpace_A": "s1", "IfcSpace_B": "s2", "IfcSpace_C": "s3",
    "IfcDoor_1": "d1", "IfcDoor_2": "d2", "IfcDoor_3": "d3",
}

CAMS = {
    1: {"device": "SmartCamera_1", "space": "IfcSpace_A",
        "rois": [{"roi_id": "R1", "xywh": [100, 100, 60, 200], "door": "IfcDoor_1"}]},
    2: {"device": "SmartCamera_2", "space": "IfcSpace_A",
        "rois": [{"roi_id": "R2", "xywh": [900, 80, 50, 180], "door": "IfcDoor_3"}]},
    3: {"device": "SmartCamera_3", "space": "IfcSpace_B",
        "rois": [{"roi_id": "R3", "xywh": [400, 120, 70, 220], "door": "IfcDoor_1"},
                 {"roi_id": "R4", "xywh": [700, 120, 70, 220], "door": "IfcDoor_2"}]},
    4: {"device": "SmartCamera_4", "space": "IfcSpace_C",
        "rois": [{"roi_id": "R5", "xywh": [200, 150, 80, 240], "door": "IfcDoor_2"}]},
}
ALL_CAMS = [1, 2, 3, 4]
CAMERA_IDS = {n: "WN-S1-C%02d" % n for n in ALL_CAMS}


def model():
    return wn.build_model(CAMS, ALL_CAMS, ELEMENT_IDS)


def zones(door_mode="none"):
    return wn.build_zones(model(), CAMS, ALL_CAMS, CAMERA_IDS, WIDTH, HEIGHT,
                          door_mode)


def topology_yaml():
    return wn.build_topology_yaml(model(), CAMS, ALL_CAMS, CAMERA_IDS,
                                  set_no=1, max_transit=120.0)


# -- rooms ------------------------------------------------------------------

def test_two_cameras_in_one_space_produce_one_area():
    areas = wn.build_areas(model())
    by_id = {a["area_id"]: a for a in areas}
    assert set(by_id) == {"WN-SPACE-1", "WN-SPACE-2", "WN-SPACE-3"}
    # The room the double counting came from: one area, two cameras.
    assert by_id["WN-SPACE-1"]["_cameras"] == [1, 2]
    assert by_id["WN-SPACE-2"]["_cameras"] == [3]


def test_area_id_follows_gt_json_space_numbering():
    # gt.json says "space 2"; joining to it later must not need a lookup table.
    assert wn.area_id_for("IfcSpace_B", ELEMENT_IDS) == "WN-SPACE-2"
    # No abbreviation in element_IDs.txt -> fall back to the raw IFC id rather
    # than inventing a number.
    assert wn.area_id_for("IfcSpace_Z", {}) == "WN-IfcSpace_Z"


def test_area_sqm_is_zero_not_guessed():
    # The floor plan is in the IFC file, which this script does not parse. 0
    # disables density/capacity rules; a made-up number would silently drive them.
    for area in wn.build_areas(model()):
        assert area["area_sqm"] == 0.0
        assert area["capacity_max"] == 0


# -- zone mapping -----------------------------------------------------------

def test_both_cameras_of_a_room_map_their_zone_to_the_same_area():
    z = zones()
    assert z["WN-S1-C01"][0]["physical_area_id"] == "WN-SPACE-1"
    assert z["WN-S1-C02"][0]["physical_area_id"] == "WN-SPACE-1"
    assert z["WN-S1-C03"][0]["physical_area_id"] == "WN-SPACE-2"


def test_default_emits_room_zones_only():
    # Door regions need a human to rule on; the room mapping does not, so the
    # default lands the part that is safe on its own.
    for camera_zones in zones("none").values():
        assert [z["zone_type"] for z in camera_zones] == ["MONITORED"]


def test_door_mode_puts_doors_before_the_room_zone():
    # zone_of() returns the FIRST match after lifting restricted zones. Neither
    # is restricted, so this ordering is the only thing that lets a doorway win.
    z = zones("door")["WN-S1-C03"]
    assert [x["zone_type"] for x in z] == ["DOOR", "DOOR", "MONITORED"]

    parsed = [zone_from_dict(x, WIDTH, HEIGHT) for x in z]
    assert zone_of((430, 300), parsed) == "WN-S1-C03-DOOR-D1"   # inside door 1
    assert zone_of((730, 300), parsed) == "WN-S1-C03-DOOR-D2"   # inside door 2
    assert zone_of((50, 700), parsed) == "WN-S1-C03-ROOM"       # ordinary floor


def test_mask_mode_makes_doorways_detection_masks_with_no_area():
    z = zones("mask")["WN-S1-C03"]
    assert [x["zone_type"] for x in z] == ["UNMONITORED", "UNMONITORED",
                                           "MONITORED"]
    # Not mapped to an area on purpose: whoever stands there is in the NEXT
    # room, so attributing them to this one is the error being avoided.
    assert z[0]["physical_area_id"] is None
    assert z[-1]["physical_area_id"] == "WN-SPACE-2"


def test_room_zone_covers_the_whole_frame():
    # The camera is inside the space (isHostedBy), so everything it sees is that
    # space. It also gives presence.py the floor just inside a door, without
    # which a crossing reads as "door -> nowhere" and is dropped.
    room = zones()["WN-S1-C01"][0]
    assert room["polygon"] == [[0, 0], [WIDTH, 0], [WIDTH, HEIGHT], [0, HEIGHT]]


@pytest.mark.parametrize("mode", ["none", "door", "mask"])
def test_generated_zone_payloads_pass_the_api_validator(mode):
    for camera_id, camera_zones in zones(mode).items():
        payload = {"camera_id": camera_id,
                   "zones": wn._strip_private(camera_zones)}
        ok, errors = validate_zones(payload)
        assert ok, (camera_id, errors)


def test_private_keys_never_reach_the_api():
    # The _-prefixed keys are provenance for humans reading the plan file.
    sent = wn._strip_private(zones("door")["WN-S1-C03"])
    assert all(not k.startswith("_") for z in sent for k in z)
    assert any(k.startswith("_") for z in zones("door")["WN-S1-C03"] for k in z)


# -- topology ---------------------------------------------------------------

def test_same_room_pair_is_declared_overlapping(tmp_path):
    path = tmp_path / "topology.yaml"
    path.write_text(topology_yaml(), encoding="utf-8")
    topo = CameraTopology.load(str(path))

    assert topo.is_overlapping("WN-S1-C01", "WN-S1-C02")
    # The case the whole thing exists for: two cameras on one room see the same
    # person at the same instant, and that must read as evidence FOR a match.
    ok, reason = topo.feasible("WN-S1-C01", "WN-S1-C02", 0.0)
    assert (ok, reason) == (True, "overlapping")


def test_adjacent_rooms_get_a_transit_not_an_overlap(tmp_path):
    path = tmp_path / "topology.yaml"
    path.write_text(topology_yaml(), encoding="utf-8")
    topo = CameraTopology.load(str(path))

    assert not topo.is_overlapping("WN-S1-C01", "WN-S1-C03")
    ok, reason = topo.feasible("WN-S1-C01", "WN-S1-C03", 3.0)
    assert (ok, reason) == (True, "transit_ok")   # known pair, not a fallback


def test_non_adjacent_rooms_are_left_for_a_human(tmp_path):
    # Spaces A and C share no door. Inventing a minimum transit would be survey
    # data this script does not have, so the pair is a comment, not an entry.
    text = topology_yaml()
    assert "NEEDS A HUMAN" in text
    assert "s1 <-> s3 : 2 doorways apart" in text

    path = tmp_path / "topology.yaml"
    path.write_text(text, encoding="utf-8")
    topo = CameraTopology.load(str(path))
    assert not topo.is_known_pair("WN-S1-C01", "WN-S1-C04")


def test_topology_records_how_it_was_generated():
    # A generated file that does not say so gets hand-edited and then silently
    # overwritten on the next run.
    text = topology_yaml()
    assert "GENERATED" in text
    assert "import_wisenet_topology.py" in text


# -- the behaviour all of the above exists to produce ------------------------

def _registry():
    reg = AreaRegistry([area_from_dict(wn._strip_private(a))
                        for a in wn.build_areas(model())])
    rows = []
    for camera_id, camera_zones in zones().items():
        for z in camera_zones:
            row = wn._strip_private(z)
            row["camera_id"] = camera_id
            rows.append(row)
    reg.load_zone_rows(rows)
    return reg


def test_three_people_on_two_cameras_of_one_room_count_as_three():
    ao = AreaOccupancy(_registry())
    people = ["gp_aaa", "gp_bbb", "gp_ccc"]
    ao.observe("WN-S1-C01", "WN-S1-C01-ROOM", people, ts=100.0)
    ao.observe("WN-S1-C02", "WN-S1-C02-ROOM", people, ts=100.1)

    assert ao.occupancy("WN-SPACE-1", 100.1) == 3          # not 6
    # Both cameras still report having seen people — the de-duplication happens
    # in the room total, not by discarding a camera.
    assert [o["observed"] for o in
            ao.camera_observations("WN-SPACE-1", 100.1)] == [3, 3]


def test_unresolved_identities_over_count_rather_than_merging():
    # The deliberate bias, inherited from globalid.py: a split is a quiet metrics
    # error, a wrong merge puts a stranger under someone else's ref.
    ao = AreaOccupancy(_registry())
    ao.observe("WN-S1-C01", "WN-S1-C01-ROOM",
               [area_ref("WN-S1-C01", t) for t in (1, 2, 3)], ts=100.0)
    ao.observe("WN-S1-C02", "WN-S1-C02-ROOM",
               [area_ref("WN-S1-C02", t) for t in (7, 8, 9)], ts=100.1)
    assert ao.occupancy("WN-SPACE-1", 100.1) == 6


def test_different_rooms_are_not_merged():
    ao = AreaOccupancy(_registry())
    ao.observe("WN-S1-C01", "WN-S1-C01-ROOM", ["gp_aaa"], ts=100.0)
    ao.observe("WN-S1-C03", "WN-S1-C03-ROOM", ["gp_zzz"], ts=100.0)
    assert ao.occupancy("WN-SPACE-1", 100.0) == 1
    assert ao.occupancy("WN-SPACE-2", 100.0) == 1


# -- the video -> camera mapping the areas hang on ---------------------------

def test_device_mapping_verification_catches_a_mismatch(tmp_path):
    d = tmp_path / "manual_annotations" / "people_detection" / "set_1"
    d.mkdir(parents=True)
    (d / "video1_1.json").write_text(json.dumps(
        {"frames": [{"deviceID": "SmartCamera_1"}]}), encoding="utf-8")
    # Filename says camera 2, annotation says camera 3.
    (d / "video1_2.json").write_text(json.dumps(
        {"frames": [{"deviceID": "SmartCamera_3"}]}), encoding="utf-8")
    # No detections at all: carries no evidence, so not a failure.
    (d / "video1_3.json").write_text(json.dumps({"frames": []}), encoding="utf-8")

    confirmed, silent, mismatches = wn.verify_device_mapping(str(tmp_path), 1)
    assert [i for i, _ in confirmed] == [1]
    assert [i for i, _ in silent] == [3]
    assert len(mismatches) == 1
    assert "video1_2.json" in mismatches[0]


def test_resolution_follows_the_set_number():
    # Sets 1-4 are 720p, 5-11 are 480p, and the door boxes differ between the
    # two calibrations — picking the wrong one misplaces every door.
    assert wn.resolution_for_set(1) == (1280, 720)
    assert wn.resolution_for_set(4) == (1280, 720)
    assert wn.resolution_for_set(5) == (640, 480)
    assert wn.resolution_for_set(11) == (640, 480)
