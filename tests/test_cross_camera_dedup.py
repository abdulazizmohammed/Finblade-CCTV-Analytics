"""Cross-camera de-duplication, end to end: two tracks -> one person -> one count.

test_areas.py proves the counting once identities exist. This proves the layer
underneath: that the real GlobalIdentityRegistry actually resolves CAM-04 track
4 and CAM-05 track 7 to one global ref, and that the resulting area occupancy
is 1 rather than 2.

The seven scenarios are those requested for overlapping cameras on one office.
"""

import pytest

from finblade.appearance import TrackFeatureBank
from finblade.areas import AreaOccupancy, AreaRegistry, PhysicalArea
from finblade.globalid import GlobalIdentityRegistry
from finblade.topology import CameraTopology

DIM = 32
OFFICE = "OFFICE_01"


def vec(*leading):
    """A unit-ish embedding whose direction is set by the leading components."""
    v = [0.0] * DIM
    for i, x in enumerate(leading):
        v[i] = float(x)
    return v


def bank(*vectors, source=None):
    b = TrackFeatureBank(capacity=5)
    for v in vectors:
        b.add(v, source=source)
    return b


def office_topology(overlapping=True):
    """CAM-04 and CAM-05 watch one room, so simultaneous sightings are expected."""
    return CameraTopology(
        overlapping=[("CAM-04", "CAM-05")] if overlapping else [],
        transits={} if overlapping else {("CAM-04", "CAM-05"): (10.0, 90.0)},
        allow_unknown_pairs=False,
    )


def registry(overlapping=True, **kw):
    return GlobalIdentityRegistry(topology=office_topology(overlapping), **kw)


def area_tracker():
    reg = AreaRegistry([PhysicalArea(area_id=OFFICE, name="Office 1")])
    reg.map_zone("CAM-04", "ZONE-04", OFFICE)     # zone "office-1"
    reg.map_zone("CAM-05", "ZONE-05", OFFICE)     # zone "office-01-02"
    return AreaOccupancy(reg)


# -- TEST 1 — same person, two overlapping cameras -------------------------

def test_same_person_two_cameras_resolves_to_one_global_id_and_occupancy_one():
    gid = registry()
    # Two views of one person: same direction, small angular difference.
    a = gid.resolve("CAM-04", 4, bank(vec(1.0, 0.05), vec(1.0, 0.02)), now=100.0,
                    zone_id="ZONE-04")
    b = gid.resolve("CAM-05", 7, bank(vec(1.0, 0.06), vec(1.0, 0.03)), now=100.2,
                    zone_id="ZONE-05")

    assert b.matched is True
    assert a.global_ref == b.global_ref, "CAM-04/4 and CAM-05/7 are one person"
    assert gid.site_occupancy() == 1

    ao = area_tracker()
    ao.observe("CAM-04", "ZONE-04", [a.global_ref], ts=100.0)
    ao.observe("CAM-05", "ZONE-05", [b.global_ref], ts=100.2)
    assert ao.occupancy(OFFICE, 100.2) == 1

    # Camera-level debugging still shows both saw somebody.
    assert [o["observed"] for o in ao.camera_observations(OFFICE, 100.2)] == [1, 1]


# -- TEST 2 — two different people -----------------------------------------

def test_two_different_people_stay_two():
    gid = registry()
    a = gid.resolve("CAM-04", 4, bank(vec(1.0, 0.0), vec(1.0, 0.0)), now=100.0)
    b = gid.resolve("CAM-05", 7, bank(vec(0.0, 1.0), vec(0.0, 1.0)), now=100.1)

    assert a.global_ref != b.global_ref
    assert gid.site_occupancy() == 2

    ao = area_tracker()
    ao.observe("CAM-04", "ZONE-04", [a.global_ref], ts=100.0)
    ao.observe("CAM-05", "ZONE-05", [b.global_ref], ts=100.1)
    assert ao.occupancy(OFFICE, 100.1) == 2


# -- TEST 3 — visible in one camera only ------------------------------------

def test_one_camera_only_counts_once():
    gid = registry()
    a = gid.resolve("CAM-04", 4, bank(vec(1.0, 0.0), vec(1.0, 0.0)), now=100.0)
    ao = area_tracker()
    ao.observe("CAM-04", "ZONE-04", [a.global_ref], ts=100.0)
    ao.observe("CAM-05", "ZONE-05", [], ts=100.0)
    assert ao.occupancy(OFFICE, 100.0) == 1


# -- TEST 4 — walks from one camera's view into the other's -----------------

def test_person_moving_between_cameras_keeps_one_identity():
    gid = registry()
    a = gid.resolve("CAM-04", 4, bank(vec(1.0, 0.04), vec(1.0, 0.05)), now=100.0)
    gid.release("CAM-04", 4)                      # leaves CAM-04's view
    b = gid.resolve("CAM-05", 7, bank(vec(1.0, 0.03), vec(1.0, 0.06)), now=103.0)

    assert a.global_ref == b.global_ref
    assert gid.site_occupancy() == 1


# -- TEST 5 — the local tracker loses the person and renumbers them ---------

def test_new_local_track_id_reconnects_to_the_same_person():
    """ByteTrack breaking an id must not create a second person."""
    gid = registry()
    a = gid.resolve("CAM-04", 4, bank(vec(1.0, 0.04), vec(1.0, 0.05)), now=100.0)
    gid.release("CAM-04", 4)                      # occlusion; id dropped
    b = gid.resolve("CAM-04", 99, bank(vec(1.0, 0.05), vec(1.0, 0.04)), now=102.0)

    assert a.global_ref == b.global_ref
    assert gid.site_occupancy() == 1


# -- TEST 6 — two people in similar clothing --------------------------------

def test_similar_clothing_alone_does_not_merge_two_people():
    """Proven-distinct people must survive looking alike.

    Both are tracked on CAM-04 at the same moment, which is physical proof they
    are two people however similar the crops are. That evidence must outweigh
    appearance.
    """
    gid = registry()
    a = gid.resolve("CAM-04", 4, bank(vec(1.0, 0.02), vec(1.0, 0.03)), now=100.0)
    b = gid.resolve("CAM-04", 5, bank(vec(1.0, 0.03), vec(1.0, 0.02)), now=100.0)
    assert a.global_ref != b.global_ref, "co-present on one camera = two people"

    # Now one of them appears on CAM-05 wearing the same uniform. It must not
    # collapse the two into one.
    gid.resolve("CAM-05", 7, bank(vec(1.0, 0.025), vec(1.0, 0.025)), now=100.1)
    assert gid.site_occupancy() >= 2


def test_ambiguous_candidates_split_rather_than_guess():
    """Two candidates within the margin -> a new identity, not a coin toss."""
    gid = registry(threshold=0.5, margin=0.5)     # margin impossible to clear
    a = gid.resolve("CAM-04", 1, bank(vec(1.0, 0.0), vec(1.0, 0.0)), now=100.0)
    b = gid.resolve("CAM-04", 2, bank(vec(0.99, 0.1), vec(0.99, 0.1)), now=100.0)
    c = gid.resolve("CAM-05", 9, bank(vec(1.0, 0.05), vec(1.0, 0.05)), now=100.1)

    assert c.global_ref not in (a.global_ref, b.global_ref)
    assert c.matched is False
    assert c.reason in ("ambiguous_margin", "below_threshold")


# -- TEST 7 — unrelated cameras --------------------------------------------

def test_unrelated_cameras_are_not_compared_however_alike_they_look():
    """Identical embeddings must not merge across a pair with no route."""
    topo = CameraTopology(overlapping=[("CAM-04", "CAM-05")],
                          transits={}, allow_unknown_pairs=False)
    gid = GlobalIdentityRegistry(topology=topo)
    a = gid.resolve("CAM-04", 4, bank(vec(1.0, 0.0), vec(1.0, 0.0)), now=100.0)
    # CAM-99 is in no topology entry and unknown pairs are refused.
    b = gid.resolve("CAM-99", 7, bank(vec(1.0, 0.0), vec(1.0, 0.0)), now=100.1)

    assert a.global_ref != b.global_ref
    assert gid.stats["rejected_topology"] >= 1


def test_overlapping_pair_must_be_declared_for_simultaneous_matching():
    """The config failure that silently restores double-counting.

    Configured as a WALK, dt~0 is 'two places at once' and every correct match
    is refused before appearance is scored — the room goes back to counting one
    person twice, with nothing in the logs to say why.
    """
    same = (bank(vec(1.0, 0.04), vec(1.0, 0.05)),
            bank(vec(1.0, 0.05), vec(1.0, 0.04)))

    good = registry(overlapping=True)
    a = good.resolve("CAM-04", 4, same[0], now=100.0)
    b = good.resolve("CAM-05", 7, same[1], now=100.1)
    assert a.global_ref == b.global_ref
    assert good.site_occupancy() == 1

    bad = registry(overlapping=False)      # declared as a 10-90s walk instead
    c = bad.resolve("CAM-04", 4, same[0], now=100.0)
    d = bad.resolve("CAM-05", 7, same[1], now=100.1)
    assert c.global_ref != d.global_ref
    assert bad.site_occupancy() == 2       # the double count, restored
    assert bad.stats["rejected_topology"] >= 1


# -- viewpoint diversity in the feature bank -------------------------------

def test_one_camera_cannot_crowd_every_other_viewpoint_out_of_the_bank():
    """The reported symptom: different ids across angles, correct id on re-entry.

    The binding is sticky, so the camera holding a track pushes a fresh view in
    on every resolve. With a plain oldest-out FIFO the bank ends up holding
    nothing but that camera's angle, and the second camera's view of the same
    person then has only front views to score against.
    """
    b = TrackFeatureBank(capacity=5)
    b.add(vec(1.0, 0.0), source="CAM-05")           # the one rear view
    for _ in range(8):                              # CAM-04 keeps resolving
        b.add(vec(0.0, 1.0), source="CAM-04")

    mix = b.source_mix()
    assert mix.get("CAM-05", 0) >= 1, "the other camera's view must survive"
    assert mix["CAM-04"] <= 4
    assert b.n == 5


def test_bank_without_source_labels_behaves_as_before():
    b = TrackFeatureBank(capacity=3)
    for i in range(5):
        b.add(vec(float(i), 1.0))
    assert b.n == 3
    assert b.source_mix() == {None: 3}


def test_cross_angle_match_survives_a_camera_monopolising_the_bank():
    """End to end: the office case that was minting a second identity."""
    gid = registry()
    front, rear = vec(1.0, 0.0), vec(0.86, 0.51)     # ~30 degrees apart

    # The person is picked up by CAM-04 and stays in its view for a while.
    r = gid.resolve("CAM-04", 4, bank(front, front), now=100.0)
    for t in range(1, 9):
        gid.resolve("CAM-04", 4, bank(front), now=100.0 + t)

    ident = gid.get(r.global_ref)
    assert ident.bank.source_mix() == {"CAM-04": 5}

    # CAM-05 now sees the same person from the other side. With the bank full
    # of CAM-04 views this is the hard case; what matters is that whichever way
    # it resolves, a REAR view is retained afterwards so the next attempt has
    # something comparable to match against.
    gid.resolve("CAM-05", 7, bank(rear, rear), now=109.0)
    banks = [i.bank.source_mix() for i in
             (gid.get(x) for x in gid.all_refs()) if i]
    assert any("CAM-05" in m for m in banks)


# -- the match decision is auditable ---------------------------------------

def test_a_match_reports_why_it_was_made():
    gid = registry()
    gid.resolve("CAM-04", 4, bank(vec(1.0, 0.04), vec(1.0, 0.05)), now=100.0)
    r = gid.resolve("CAM-05", 7, bank(vec(1.0, 0.05), vec(1.0, 0.04)), now=100.1)
    d = r.as_dict()
    assert d["matched"] is True
    assert d["reason"] in ("appearance_match", "consolidated_duplicate")
    assert 0.0 <= d["score"] <= 1.0
    assert "runner_up" in d and "candidates" in d


def test_near_misses_are_counted_so_the_threshold_can_be_tuned():
    """A refused match must be measurable, not just invisible.

    Without this a site whose real cross-angle scores sit at 0.66 looks
    identical to a site where ReID is working: both show identities being
    created. The count and the best rejected score are what distinguish
    "threshold too high" from "genuinely different people".
    """
    gid = registry(threshold=0.99, margin=0.0)     # refuse almost everything
    gid.resolve("CAM-04", 4, bank(vec(1.0, 0.0), vec(1.0, 0.0)), now=100.0)
    gid.resolve("CAM-05", 7, bank(vec(0.86, 0.51), vec(0.86, 0.51)), now=100.1)

    assert gid.stats["below_threshold"] >= 1
    assert 0.0 < gid.stats["best_rejected_score"] < 0.99
