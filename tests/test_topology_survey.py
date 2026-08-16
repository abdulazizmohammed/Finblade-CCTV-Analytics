"""Deriving a camera topology from observed sightings."""

import pytest

from finblade.topology import CameraTopology
from finblade.topology_survey import (
    classify_pair,
    pair_gaps,
    propose,
    to_yaml,
)


def sightings(*rows):
    return list(rows)


# --- gaps -----------------------------------------------------------------

def test_gaps_are_between_consecutive_sightings_only():
    """First-to-last would turn a whole shift into one absurd transit."""
    s = sightings(("p1", "CAM-A", 0.0), ("p1", "CAM-B", 10.0),
                  ("p1", "CAM-C", 25.0))
    g = pair_gaps(s)
    assert g[("CAM-A", "CAM-B")] == [10.0]
    assert g[("CAM-B", "CAM-C")] == [15.0]
    assert ("CAM-A", "CAM-C") not in g          # never adjacent


def test_same_camera_sightings_produce_no_gap():
    s = sightings(("p1", "CAM-A", 0.0), ("p1", "CAM-A", 5.0))
    assert pair_gaps(s) == {}


def test_pairs_are_undirected():
    s = sightings(("p1", "CAM-B", 0.0), ("p1", "CAM-A", 3.0),
                  ("p2", "CAM-A", 0.0), ("p2", "CAM-B", 4.0))
    g = pair_gaps(s)
    assert sorted(g) == [("CAM-A", "CAM-B")]
    assert sorted(g[("CAM-A", "CAM-B")]) == [3.0, 4.0]


def test_people_are_kept_separate():
    """Two people's positions must never be chained into one journey."""
    s = sightings(("p1", "CAM-A", 0.0), ("p2", "CAM-B", 1.0),
                  ("p1", "CAM-A", 2.0), ("p2", "CAM-B", 3.0))
    assert pair_gaps(s) == {}


# --- classification -------------------------------------------------------

def test_simultaneous_sightings_read_as_overlapping():
    v = classify_pair([0.0, 0.1, 0.0, 0.3, 0.2, 0.0])
    assert v["kind"] == "overlapping"
    assert v["near_zero_fraction"] == 1.0


def test_consistent_walk_reads_as_a_transit_window():
    v = classify_pair([14.0, 17.0, 15.0, 22.0, 16.0, 19.0])
    assert v["kind"] == "transit"
    assert v["min_seconds"] >= 14.0        # not below the fastest real walk
    assert v["max_seconds"] >= v["min_seconds"]


def test_one_bad_zero_does_not_collapse_the_minimum():
    """A single wrong match at dt=0 would otherwise disable the gate."""
    v = classify_pair([0.0, 14.0, 15.0, 16.0, 17.0, 18.0, 19.0, 20.0])
    assert v["kind"] == "transit"
    assert v["min_seconds"] > 0.0


def test_too_few_samples_is_not_a_measurement():
    v = classify_pair([15.0, 16.0])
    assert v["kind"] == "insufficient"
    assert "min_seconds" not in v          # refuses to invent a window


def test_no_samples_is_no_data():
    assert classify_pair([])["kind"] == "no_data"


# --- proposal -------------------------------------------------------------

def test_proposal_separates_overlap_from_walk():
    s = []
    for i in range(8):                      # A/B share floor
        s += [("p%d" % i, "CAM-A", i * 100.0), ("p%d" % i, "CAM-B", i * 100.0 + 0.2)]
    for i in range(8):                      # B/C are a walk apart
        s += [("q%d" % i, "CAM-B", i * 100.0), ("q%d" % i, "CAM-C", i * 100.0 + 15.0)]

    p = propose(s)
    ov = {(e["a"], e["b"]) for e in p["overlapping_pairs"]}
    tr = {(e["a"], e["b"]) for e in p["transits"]}
    assert ("CAM-A", "CAM-B") in ov
    assert ("CAM-B", "CAM-C") in tr


def test_pairs_with_no_evidence_are_listed_not_omitted():
    """An omitted pair falls back to the permissive default and fails quietly."""
    s = [("p%d" % i, "CAM-A", i * 10.0) for i in range(3)]
    p = propose(s, cameras=["CAM-A", "CAM-B", "CAM-C"])
    unsurveyed = {(e["a"], e["b"]) for e in p["unsurveyed"]}
    assert ("CAM-A", "CAM-B") in unsurveyed
    assert ("CAM-B", "CAM-C") in unsurveyed


def test_proposal_round_trips_into_a_working_topology():
    """The generated YAML must actually load and gate as intended."""
    yaml = pytest.importorskip("yaml")
    s = []
    for i in range(8):
        s += [("p%d" % i, "CAM-A", i * 100.0), ("p%d" % i, "CAM-B", i * 100.0 + 0.2)]
    for i in range(8):
        s += [("q%d" % i, "CAM-B", i * 100.0), ("q%d" % i, "CAM-C", i * 100.0 + 15.0)]

    topo = CameraTopology.from_dict(yaml.safe_load(to_yaml(propose(s))))

    # The overlapping pair must accept simultaneous sightings...
    assert topo.is_overlapping("CAM-A", "CAM-B")
    assert topo.feasible("CAM-A", "CAM-B", 0.0)[0] is True
    # ...and the walk must reject someone appearing instantly at the far end.
    assert topo.feasible("CAM-B", "CAM-C", 0.5)[0] is False
    assert topo.feasible("CAM-B", "CAM-C", 16.0)[0] is True


def test_yaml_records_the_evidence_for_every_entry():
    """A window with no sample count behind it is indistinguishable from a guess."""
    s = []
    for i in range(8):
        s += [("p%d" % i, "CAM-A", i * 100.0), ("p%d" % i, "CAM-B", i * 100.0 + 0.2)]
    text = to_yaml(propose(s), site="SITE-A")
    assert "SITE-A" in text
    assert "8 handovers" in text
    assert "PROPOSED FROM OBSERVED DATA" in text


def test_empty_input_still_produces_a_reviewable_file():
    text = to_yaml(propose([], cameras=["CAM-A", "CAM-B"]))
    assert "overlapping_pairs:" in text
    assert "CAM-A <-> CAM-B" in text        # listed as needing a human
