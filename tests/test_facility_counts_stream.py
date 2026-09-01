"""Part C: merged facility counts published to fb:facility.

Two things under test. The gate — counts publish on change, not on every tick,
with a keepalive so a quiet building is distinguishable from a dead publisher.
And the separation — counts go to their own stream and do not contaminate
fb:events, which a consumer of either would otherwise have to filter.

Nothing here counts detections. The roster counts door crossings by identity;
these tests drive it through the same ingest path a camera worker uses.
"""

import unittest

from finblade.emission import StateWriteGate
from finblade.events import ZONE_ENTRY, ZONE_TRANSITION, new_event
from finblade.identity import PersonRefHasher
from services.api.bus import EVENTS_STREAM, FACILITY_STREAM, InMemoryBus
from services.api.service import FACILITY_COUNTS, IngestService
from services.api.store import InMemoryStore

H = PersonRefHasher(session_salt="fixed")

DOOR = "ZONE-DOOR"
INSIDE = "ZONE-LOBBY"
OUTSIDE = "ZONE-STREET"

# A two-way DOOR resolves on DEPARTURE, from the zones either side of it — so
# the fixture needs interior floor AND declared ground beyond the boundary, or
# every crossing is ambiguous rather than counted. See DoorPolicy in
# finblade/presence.py; this is the coverage requirement it documents.
ZONES = [
    {"zone_id": DOOR, "zone_name": "Door", "camera_id": "CAM-01",
     "zone_type": "DOOR", "polygon": [[0, 0], [1, 0], [1, 1]]},
    {"zone_id": INSIDE, "zone_name": "Lobby", "camera_id": "CAM-01",
     "zone_type": "MONITORED", "polygon": [[0, 0], [1, 0], [1, 1]]},
    {"zone_id": OUTSIDE, "zone_name": "Street", "camera_id": "CAM-01",
     "zone_type": "OUTSIDE", "polygon": [[0, 0], [1, 0], [1, 1]]},
]


def make_svc(**gate_kw):
    store = InMemoryStore()
    store.save_zones("CAM-01", ZONES)
    gate = StateWriteGate(gate_kw.pop("mode", "change"),
                          gate_kw.pop("keepalive_s", 300.0))
    return IngestService(store, InMemoryBus(), counts_gate=gate)


def _move(svc, ref, frm, to, ts):
    svc.ingest_event(new_event(ZONE_TRANSITION, "CAM-01", "SITE-1", ts,
                               zone_from=frm, zone_to=to, person_ref=ref))


def enter(svc, who, ts):
    """Street -> door -> lobby. Resolves as ADMIT on arrival inside."""
    ref = H.ref(who)
    _move(svc, ref, OUTSIDE, DOOR, ts)
    _move(svc, ref, DOOR, INSIDE, ts + 1)
    return ref


def leave(svc, ref, ts):
    """Lobby -> door -> street. Resolves as DISCHARGE on arrival beyond."""
    _move(svc, ref, INSIDE, DOOR, ts)
    _move(svc, ref, DOOR, OUTSIDE, ts + 1)


def counts(svc):
    return svc.bus.consume_from(FACILITY_STREAM)


class TestStreamSeparation(unittest.TestCase):
    def test_counts_go_to_their_own_stream(self):
        svc = make_svc()
        svc.publish_facility_counts(now=1000.0, force=True)
        self.assertEqual(len(counts(svc)), 1)
        # ...and not into the event firehose.
        self.assertEqual(svc.bus.consume_from(EVENTS_STREAM), [])

    def test_events_still_go_to_the_default_stream(self):
        svc = make_svc()
        enter(svc, 1, 1000.0)
        self.assertTrue(svc.bus.consume())               # fb:events, unchanged
        self.assertEqual(svc.bus.consume(),
                         svc.bus.consume_from(EVENTS_STREAM))

    def test_record_is_marked_and_versioned(self):
        svc = make_svc()
        rec = svc.publish_facility_counts(now=1000.0, force=True)
        self.assertEqual(rec["record_type"], FACILITY_COUNTS)
        self.assertEqual(rec["schema_version"], 1)

    def test_record_carries_the_merged_decomposition(self):
        svc = make_svc()
        rec = svc.publish_facility_counts(now=1000.0, force=True)
        for key in ("occupancy", "observed", "baseline", "pending_crossings",
                    "doors", "stats", "ts"):
            self.assertIn(key, rec)

    def test_no_bus_is_not_an_error(self):
        svc = IngestService(InMemoryStore(), None)
        self.assertIsNone(svc.publish_facility_counts(now=1000.0, force=True))


class TestGate(unittest.TestCase):
    def test_unchanged_occupancy_is_suppressed(self):
        svc = make_svc()
        svc.publish_facility_counts(now=1000.0)
        svc.publish_facility_counts(now=1005.0)
        svc.publish_facility_counts(now=1010.0)
        # First publish anchors the stream; the two repeats say nothing new.
        self.assertEqual(len(counts(svc)), 1)

    def test_keepalive_republishes_an_unchanged_count(self):
        # The reason the keepalive exists: without it, "nothing changed" and
        # "the publisher died" look identical on the stream.
        svc = make_svc(keepalive_s=300.0)
        svc.publish_facility_counts(now=1000.0)
        svc.publish_facility_counts(now=1200.0)          # inside the window
        self.assertEqual(len(counts(svc)), 1)
        svc.publish_facility_counts(now=1301.0)          # past it
        self.assertEqual(len(counts(svc)), 2)

    def test_keepalive_of_zero_disables_republishing(self):
        svc = make_svc(keepalive_s=0.0)
        svc.publish_facility_counts(now=1000.0)
        svc.publish_facility_counts(now=99999.0)
        self.assertEqual(len(counts(svc)), 1)

    def test_always_mode_publishes_every_tick(self):
        svc = make_svc(mode="always")
        for ts in (1000.0, 1005.0, 1010.0):
            svc.publish_facility_counts(now=ts)
        self.assertEqual(len(counts(svc)), 3)

    def test_force_bypasses_the_gate(self):
        svc = make_svc()
        svc.publish_facility_counts(now=1000.0)
        svc.publish_facility_counts(now=1001.0, force=True)
        self.assertEqual(len(counts(svc)), 2)

    def test_stale_creeping_upward_does_not_republish(self):
        # `stale` grows with the clock alone. If it were in the change key the
        # gate would republish on every tick forever. Keepalive off so the only
        # thing that could republish here is the change key.
        svc = make_svc(keepalive_s=0.0)
        enter(svc, 1, 1000.0)
        before = len(counts(svc))
        for ts in (5000.0, 9000.0, 20000.0):
            svc.publish_facility_counts(now=ts)
        self.assertEqual(len(counts(svc)), before)


class TestCrossingsDriveTheStream(unittest.TestCase):
    def test_an_entry_publishes_a_new_count_immediately(self):
        svc = make_svc()
        enter(svc, 1, 1000.0)
        recs = counts(svc)
        self.assertTrue(recs)
        self.assertEqual(recs[-1]["occupancy"], 1)

    def test_occupancy_tracks_entries_and_exits(self):
        svc = make_svc()
        a = enter(svc, 1, 1000.0)
        enter(svc, 2, 1010.0)
        self.assertEqual(counts(svc)[-1]["occupancy"], 2)
        leave(svc, a, 1020.0)
        self.assertEqual(counts(svc)[-1]["occupancy"], 1)

    def test_two_people_produce_two_records_not_one(self):
        svc = make_svc()
        enter(svc, 1, 1000.0)
        n = len(counts(svc))
        enter(svc, 2, 1010.0)
        self.assertEqual(len(counts(svc)), n + 1)

    def test_repeated_sightings_inside_do_not_republish(self):
        # Someone walking around inside moves no count. This is the difference
        # between counting crossings and counting detections.
        svc = make_svc()
        ref = enter(svc, 1, 1000.0)
        n = len(counts(svc))
        for ts in (1005.0, 1010.0, 1015.0):
            svc.ingest_event(new_event(ZONE_ENTRY, "CAM-01", "SITE-1", ts,
                                       zone_to=INSIDE, person_ref=ref,
                                       confidence=0.9))
        self.assertEqual(len(counts(svc)), n)

    def test_baseline_change_publishes(self):
        svc = make_svc()
        svc.publish_facility_counts(now=1000.0)
        n = len(counts(svc))
        svc.set_facility_baseline({"count": 12})
        svc.publish_facility_counts(now=1005.0)
        recs = counts(svc)
        self.assertEqual(len(recs), n + 1)
        self.assertEqual(recs[-1]["occupancy"], 12)
        self.assertEqual(recs[-1]["baseline"], 12)


class TestFailureIsVisible(unittest.TestCase):
    def test_a_failing_bus_is_counted_not_raised(self):
        class Broken(InMemoryBus):
            def publish_to(self, stream, evt):
                if stream == FACILITY_STREAM:
                    raise RuntimeError("redis down")
                super().publish_to(stream, evt)

        svc = IngestService(InMemoryStore(), Broken())
        self.assertIsNone(svc.publish_facility_counts(now=1000.0, force=True))
        self.assertEqual(svc.counts_errors, 1)
        self.assertEqual(svc.counts_published, 0)

    def test_a_failing_counts_publish_does_not_break_ingest(self):
        class Broken(InMemoryBus):
            def publish_to(self, stream, evt):
                if stream == FACILITY_STREAM:
                    raise RuntimeError("redis down")
                super().publish_to(stream, evt)

        store = InMemoryStore()
        store.save_zones("CAM-01", ZONES)
        svc = IngestService(store, Broken())
        code, body = svc.ingest_event(
            new_event(ZONE_ENTRY, "CAM-01", "SITE-1", 1000.0,
                      zone_to=DOOR, person_ref=H.ref(1), confidence=0.9))
        self.assertEqual(code, 202)
        self.assertTrue(body["accepted"])

    def test_counts_stats_reports_stream_and_backend(self):
        svc = make_svc()
        svc.publish_facility_counts(now=1000.0, force=True)
        s = svc.counts_stats()
        self.assertEqual(s["stream"], FACILITY_STREAM)
        self.assertEqual(s["published"], 1)
        self.assertEqual(s["errors"], 0)
        self.assertEqual(s["bus"], "InMemoryBus")
        self.assertIn("suppressed", s["gate"])


class TestBusCompatibility(unittest.TestCase):
    def test_published_attribute_still_reads_the_default_stream(self):
        bus = InMemoryBus()
        bus.publish({"a": 1})
        self.assertEqual(len(bus.published), 1)
        self.assertEqual(bus.consume(), [{"a": 1}])

    def test_a_second_stream_does_not_appear_in_consume(self):
        bus = InMemoryBus()
        bus.publish({"a": 1})
        bus.publish_to(FACILITY_STREAM, {"b": 2})
        self.assertEqual(len(bus.consume()), 1)
        self.assertEqual(len(bus.consume_from(FACILITY_STREAM)), 1)

    def test_publish_copies_rather_than_aliases(self):
        bus = InMemoryBus()
        evt = {"a": 1}
        bus.publish(evt)
        evt["a"] = 2
        self.assertEqual(bus.consume()[0]["a"], 1)


if __name__ == "__main__":
    unittest.main()
