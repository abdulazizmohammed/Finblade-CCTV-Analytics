"""Physical-area occupancy: one room, two cameras, one person.

These are the seven acceptance scenarios for cross-camera de-duplication,
written against finblade/areas.py. The headline requirement is TEST 1: if two
cameras watch the same office and both see the same person, the office holds
one person, not two.
"""

import pytest

from finblade.areas import (
    AreaOccupancy,
    AreaRegistry,
    PhysicalArea,
    area_ref,
    distinct_occupancy,
    is_resolved,
)

OFFICE = "OFFICE-01"


def build(linger_s=4.0, ttl=30.0):
    """OFFICE-01 watched by CAM-04/ZONE-01 and CAM-05/ZONE-03."""
    reg = AreaRegistry([PhysicalArea(area_id=OFFICE, name="Office 1",
                                     capacity_max=10, area_sqm=20.0)])
    reg.map_zone("CAM-04", "ZONE-01", OFFICE)
    reg.map_zone("CAM-05", "ZONE-03", OFFICE)
    return AreaOccupancy(reg, linger_s=linger_s, observation_ttl_s=ttl)


# --------------------------------------------------------------------------
# TEST 1 — the headline: same person, two cameras, one office.
# --------------------------------------------------------------------------
def test_same_person_in_two_cameras_counts_once():
    ao = build()
    # CAM-04 track 17 and CAM-05 track 8 resolved to the same global ref.
    p001 = "gp_abc123"
    ao.observe("CAM-04", "ZONE-01", [p001], ts=100.0)
    ao.observe("CAM-05", "ZONE-03", [p001], ts=100.0)

    assert ao.occupancy(OFFICE, 100.0) == 1

    st = ao.state(OFFICE, 100.0)
    # Each camera's own observation stays visible for diagnostics...
    assert [o["observed"] for o in st["observations"]] == [1, 1]
    # ...and the naive sum is recorded so the de-duplication is measurable.
    assert st["summed_observations"] == 2
    assert st["double_counted"] == 1
    assert st["occupancy"] == 1


def test_local_track_ids_are_not_global_identities():
    """CAM-04 track 17 and CAM-05 track 8 are different numbers, one person;
    CAM-04 track 17 and CAM-05 track 17 are the same number, two people."""
    ao = build()
    # Unresolved on both cameras: same track number, must NOT merge.
    ao.observe("CAM-04", "ZONE-01", [area_ref("CAM-04", 17)], ts=10.0)
    ao.observe("CAM-05", "ZONE-03", [area_ref("CAM-05", 17)], ts=10.0)
    assert ao.occupancy(OFFICE, 10.0) == 2

    # Once ReID resolves both to one person, the office holds one.
    g = "gp_same"
    ao.observe("CAM-04", "ZONE-01", [area_ref("CAM-04", 17, g)], ts=11.0)
    ao.observe("CAM-05", "ZONE-03", [area_ref("CAM-05", 8, g)], ts=11.0)
    assert ao.occupancy(OFFICE, 11.0) == 1


def test_area_ref_scopes_unresolved_tracks_per_camera():
    assert area_ref("CAM-04", 17) != area_ref("CAM-05", 17)
    assert area_ref("CAM-04", 17, "gp_x") == area_ref("CAM-05", 8, "gp_x") == "gp_x"
    assert is_resolved("gp_x") and not is_resolved(area_ref("CAM-04", 17))


# --------------------------------------------------------------------------
# TEST 2 — different people from different cameras.
# --------------------------------------------------------------------------
def test_different_people_from_different_cameras_sum():
    ao = build()
    ao.observe("CAM-04", "ZONE-01", ["P001"], ts=100.0)
    ao.observe("CAM-05", "ZONE-03", ["P002"], ts=100.0)
    assert ao.occupancy(OFFICE, 100.0) == 2


# --------------------------------------------------------------------------
# TEST 3 — partial overlap. The case that rules out max/min/average.
# --------------------------------------------------------------------------
def test_partial_overlap_counts_union():
    ao = build()
    ao.observe("CAM-04", "ZONE-01", ["P001", "P002"], ts=100.0)
    ao.observe("CAM-05", "ZONE-03", ["P002", "P003"], ts=100.0)

    assert ao.occupancy(OFFICE, 100.0) == 3           # union, not 2 and not 4

    st = ao.state(OFFICE, 100.0)
    assert st["summed_observations"] == 4             # what summing would say
    assert max(o["observed"] for o in st["observations"]) == 2   # what MAX would say
    assert st["occupancy"] == 3


# --------------------------------------------------------------------------
# TEST 4 — one camera loses the track, the other still sees them.
# --------------------------------------------------------------------------
def test_occupancy_holds_when_one_camera_loses_the_track():
    ao = build()
    ao.observe("CAM-04", "ZONE-01", ["P001"], ts=100.0)
    ao.observe("CAM-05", "ZONE-03", ["P001"], ts=100.0)
    ao.tick(100.0)
    assert ao.occupancy(OFFICE, 100.0) == 1

    # CAM-04 drops the track; CAM-05 still has them. No dip to 0.
    ao.observe("CAM-04", "ZONE-01", [], ts=101.0)
    ao.observe("CAM-05", "ZONE-03", ["P001"], ts=101.0)
    ao.tick(101.0)
    assert ao.occupancy(OFFICE, 101.0) == 1

    # ...and it stays 1 well past the linger window, because they are visible.
    for t in (105.0, 120.0, 200.0):
        ao.observe("CAM-04", "ZONE-01", [], ts=t)
        ao.observe("CAM-05", "ZONE-03", ["P001"], ts=t)
        ao.tick(t)
        assert ao.occupancy(OFFICE, t) == 1


def test_no_entry_exit_churn_when_one_camera_loses_the_track():
    ao = build()
    ao.observe("CAM-04", "ZONE-01", ["P001"], ts=100.0)
    ao.observe("CAM-05", "ZONE-03", ["P001"], ts=100.0)
    assert [e["event"] for e in ao.transitions(100.0)] == ["AREA_ENTRY"]

    ao.observe("CAM-04", "ZONE-01", [], ts=101.0)
    ao.observe("CAM-05", "ZONE-03", ["P001"], ts=101.0)
    assert ao.transitions(101.0) == []          # nothing happened to the person


# --------------------------------------------------------------------------
# TEST 5 — walking between camera views inside one room.
# --------------------------------------------------------------------------
def test_moving_between_camera_views_inside_a_room_is_not_an_exit():
    ao = build(linger_s=4.0)
    # Visible to CAM-04 only.
    ao.observe("CAM-04", "ZONE-01", ["P001"], ts=100.0)
    ao.observe("CAM-05", "ZONE-03", [], ts=100.0)
    ao.tick(100.0)
    ao.transitions(100.0)
    assert ao.occupancy(OFFICE, 100.0) == 1

    # Crossing the blind spot between the two views: seen by NOBODY for 2s.
    ao.observe("CAM-04", "ZONE-01", [], ts=102.0)
    ao.observe("CAM-05", "ZONE-03", [], ts=102.0)
    ao.tick(102.0)
    assert ao.occupancy(OFFICE, 102.0) == 1, "blind spot must not empty the room"
    assert ao.transitions(102.0) == [], "must not emit an exit for a blind spot"

    # Reappears on CAM-05. Still one person, still no entry/exit pair.
    ao.observe("CAM-04", "ZONE-01", [], ts=103.0)
    ao.observe("CAM-05", "ZONE-03", ["P001"], ts=103.0)
    ao.tick(103.0)
    assert ao.occupancy(OFFICE, 103.0) == 1
    assert ao.transitions(103.0) == []


def test_blind_spot_grace_does_not_depend_on_calling_tick():
    """Occupancy must be the same whether or not the caller ticks.

    The linger position used to be recorded only in tick(), so an endpoint
    that read without ticking saw the person disappear while another endpoint
    still counted them — two views of one room disagreeing.
    """
    ticked, plain = build(linger_s=4.0), build(linger_s=4.0)
    for ao in (ticked, plain):
        ao.observe("CAM-04", "ZONE-01", ["P001"], ts=100.0)
    ticked.tick(100.0)

    # Now nobody can see them: a blind spot between the two camera views.
    for ao in (ticked, plain):
        ao.observe("CAM-04", "ZONE-01", [], ts=102.0)
        ao.observe("CAM-05", "ZONE-03", [], ts=102.0)
    ticked.tick(102.0)

    assert plain.occupancy(OFFICE, 102.0) == 1
    assert ticked.occupancy(OFFICE, 102.0) == plain.occupancy(OFFICE, 102.0)


# --------------------------------------------------------------------------
# TEST 6 — actually leaving the room.
# --------------------------------------------------------------------------
def test_leaving_the_room_drops_occupancy_to_zero():
    ao = build(linger_s=4.0)
    ao.observe("CAM-04", "ZONE-01", ["P001"], ts=100.0)
    ao.tick(100.0)
    ao.transitions(100.0)
    assert ao.occupancy(OFFICE, 100.0) == 1

    # Gone from every camera, and stays gone past the linger window.
    ao.observe("CAM-04", "ZONE-01", [], ts=101.0)
    ao.observe("CAM-05", "ZONE-03", [], ts=101.0)
    ao.tick(101.0)
    assert ao.occupancy(OFFICE, 106.0) == 0
    assert [e["event"] for e in ao.transitions(106.0)] == ["AREA_EXIT"]


def test_door_crossing_drops_occupancy_immediately():
    """A known door transition should not wait out the blind-spot grace."""
    ao = build(linger_s=30.0)
    ao.observe("CAM-04", "ZONE-01", ["P001"], ts=100.0)
    ao.tick(100.0)
    ao.observe("CAM-04", "ZONE-01", [], ts=101.0)
    assert ao.occupancy(OFFICE, 101.0) == 1        # still lingering

    assert ao.depart("P001", OFFICE) is True
    assert ao.occupancy(OFFICE, 101.0) == 0        # immediate


def test_moving_to_another_area_does_not_leave_them_in_the_old_one():
    reg = AreaRegistry([PhysicalArea(area_id="OFFICE-01"),
                        PhysicalArea(area_id="OFFICE-02")])
    reg.map_zone("CAM-04", "ZONE-01", "OFFICE-01")
    reg.map_zone("CAM-06", "ZONE-01", "OFFICE-02")
    ao = AreaOccupancy(reg, linger_s=30.0)

    ao.observe("CAM-04", "ZONE-01", ["P001"], ts=100.0)
    ao.tick(100.0)
    assert ao.occupancy("OFFICE-01", 100.0) == 1

    # Walks next door. Long linger must NOT keep them in both rooms at once.
    ao.observe("CAM-04", "ZONE-01", [], ts=101.0)
    ao.observe("CAM-06", "ZONE-01", ["P001"], ts=101.0)
    ao.tick(101.0)
    assert ao.occupancy("OFFICE-01", 101.0) == 0
    assert ao.occupancy("OFFICE-02", 101.0) == 1


# --------------------------------------------------------------------------
# TEST 7 — two cameras covering different parts of one room.
# --------------------------------------------------------------------------
def test_disjoint_views_of_one_room_add_up():
    ao = build()
    ao.observe("CAM-04", "ZONE-01", ["P001", "P002"], ts=100.0)
    ao.observe("CAM-05", "ZONE-03", ["P003"], ts=100.0)
    # Must NOT be capped at the busiest single camera (which would give 2).
    assert ao.occupancy(OFFICE, 100.0) == 3


# --------------------------------------------------------------------------
# Single-camera compatibility — the existing behaviour must not change.
# --------------------------------------------------------------------------
def test_single_camera_area_behaves_exactly_as_before():
    reg = AreaRegistry([PhysicalArea(area_id="AREA-01", capacity_max=4)])
    reg.map_zone("CAM-01", "ZONE-01", "AREA-01")
    ao = AreaOccupancy(reg)
    ao.observe("CAM-01", "ZONE-01", ["A", "B", "C"], ts=50.0)

    st = ao.state("AREA-01", 50.0)
    assert st["occupancy"] == 3
    assert st["summed_observations"] == 3      # nothing to de-duplicate
    assert st["double_counted"] == 0
    assert st["capacity_pct"] == 75.0


def test_unmapped_zone_is_not_an_area_and_is_ignored():
    """A camera zone with no physical area keeps working as a plain zone."""
    ao = build()
    assert ao.observe("CAM-09", "ZONE-07", ["P009"], ts=100.0) is None
    assert ao.registry.area_ids() == [OFFICE]
    assert ao.occupancy(OFFICE, 100.0) == 0


# --------------------------------------------------------------------------
# Failure modes.
# --------------------------------------------------------------------------
def test_dead_camera_stops_contributing_people():
    """A crashed worker's last observation must not pin its people forever."""
    ao = build(ttl=30.0)
    ao.observe("CAM-04", "ZONE-01", ["P001"], ts=100.0)
    ao.observe("CAM-05", "ZONE-03", ["P002"], ts=100.0)
    assert ao.occupancy(OFFICE, 100.0) == 2

    # CAM-04 dies; CAM-05 keeps reporting. After the TTL, only CAM-05 counts.
    ao.observe("CAM-05", "ZONE-03", ["P002"], ts=140.0)
    assert ao.occupancy(OFFICE, 140.0) == 1
    assert [o["stale"] for o in ao.state(OFFICE, 140.0)["observations"]] == [True, False]


def test_drop_camera_removes_its_observations():
    ao = build()
    ao.observe("CAM-04", "ZONE-01", ["P001"], ts=100.0)
    ao.observe("CAM-05", "ZONE-03", ["P002"], ts=100.0)
    assert ao.drop_camera("CAM-04") == 1
    assert ao.occupancy(OFFICE, 100.0) == 1


def test_two_zones_of_one_camera_in_one_area_do_not_double_count():
    """Splitting a room into two polygons on ONE camera is still one room."""
    reg = AreaRegistry([PhysicalArea(area_id=OFFICE)])
    reg.map_zone("CAM-04", "ZONE-01", OFFICE)
    reg.map_zone("CAM-04", "ZONE-02", OFFICE)
    ao = AreaOccupancy(reg)
    ao.observe("CAM-04", "ZONE-01", ["P001"], ts=1.0)
    ao.observe("CAM-04", "ZONE-02", ["P001"], ts=1.0)
    assert ao.occupancy(OFFICE, 1.0) == 1


def test_areas_are_explicit_never_inferred_from_names():
    """Similar names must not be merged; the mapping is by id only."""
    reg = AreaRegistry()
    reg.map_zone("CAM-04", "Z1", "OFFICE-01")
    reg.map_zone("CAM-05", "Z1", "OFFICE-02")
    ao = AreaOccupancy(reg)
    ao.observe("CAM-04", "Z1", ["P001"], ts=1.0)
    ao.observe("CAM-05", "Z1", ["P002"], ts=1.0)
    # Same zone id, similar rooms — but two declared areas, so two counts.
    assert ao.occupancy("OFFICE-01", 1.0) == 1
    assert ao.occupancy("OFFICE-02", 1.0) == 1


def test_unresolved_people_are_reported_so_a_count_can_be_judged():
    ao = build()
    ao.observe("CAM-04", "ZONE-01", ["gp_real", area_ref("CAM-04", 9)], ts=1.0)
    st = ao.state(OFFICE, 1.0)
    assert st["occupancy"] == 2
    assert st["unresolved"] == 1


# --------------------------------------------------------------------------
# Site-wide total. The headline figure had the same double-count as the room.
# --------------------------------------------------------------------------
def test_site_total_counts_a_person_seen_by_two_cameras_once():
    rows = [
        {"camera_id": "CAM-04", "zone_id": "ZONE-04", "occupancy": 1,
         "occupants": ["gp_101"]},
        {"camera_id": "CAM-05", "zone_id": "ZONE-05", "occupancy": 1,
         "occupants": ["gp_101"]},
    ]
    d = distinct_occupancy(rows)
    assert d["total"] == 1          # was 2 when this summed
    assert d["summed"] == 2
    assert d["double_counted"] == 1


def test_site_total_adds_up_genuinely_different_people():
    rows = [
        {"occupancy": 2, "occupants": ["gp_1", "gp_2"]},
        {"occupancy": 2, "occupants": ["gp_2", "gp_3"]},
    ]
    assert distinct_occupancy(rows)["total"] == 3      # not 4, not 2


def test_site_total_falls_back_to_summing_without_identities():
    """An older worker reports no occupants; its people must still count."""
    rows = [{"occupancy": 3}, {"occupancy": 2}]
    d = distinct_occupancy(rows)
    assert d["total"] == 5
    assert d["double_counted"] == 0
    assert d["identity_zones"] == 0


def test_site_total_mixes_identified_and_unidentified_zones():
    rows = [
        {"occupancy": 1, "occupants": ["gp_101"]},
        {"occupancy": 1, "occupants": ["gp_101"]},   # same person, deduped
        {"occupancy": 4},                            # no identities, added on
    ]
    d = distinct_occupancy(rows)
    assert d["total"] == 5
    assert d["distinct"] == 1
    assert d["counted_without_identity"] == 4


def test_site_total_of_nothing_is_zero():
    assert distinct_occupancy([])["total"] == 0


def test_registry_reports_which_areas_span_cameras():
    reg = AreaRegistry()
    reg.map_zone("CAM-04", "ZONE-01", OFFICE)
    reg.map_zone("CAM-05", "ZONE-03", OFFICE)
    reg.map_zone("CAM-01", "ZONE-01", "LOBBY")
    assert reg.is_multi_camera(OFFICE) is True
    assert reg.is_multi_camera("LOBBY") is False
    assert reg.zones_of(OFFICE) == [("CAM-04", "ZONE-01"), ("CAM-05", "ZONE-03")]


def test_load_zone_rows_maps_and_unmaps():
    reg = AreaRegistry()
    reg.load_zone_rows([
        {"camera_id": "CAM-04", "zone_id": "ZONE-01", "physical_area_id": OFFICE},
        {"camera_id": "CAM-05", "zone_id": "ZONE-03", "physical_area_id": OFFICE},
        {"camera_id": "CAM-09", "zone_id": "ZONE-01", "physical_area_id": None},
    ])
    assert reg.area_of("CAM-04", "ZONE-01") == OFFICE
    assert reg.area_of("CAM-09", "ZONE-01") is None
    # Detaching a zone later removes it from the area.
    reg.map_zone("CAM-05", "ZONE-03", None)
    assert reg.zones_of(OFFICE) == [("CAM-04", "ZONE-01")]
