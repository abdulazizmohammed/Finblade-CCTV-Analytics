"""Part A: FusionService accounting.

The service does not fuse anything yet, so what is tested is what it actually
promises: correct status codes, per-source tallies that separate accepted from
rejected traffic, and honest reporting that fusion is off. The last one matters
most — a fusion path reporting nothing must be distinguishable from one that is
failing, which is this project's recurring failure mode.
"""

import unittest

from finblade.observation import (
    CAMERA, FRAME_IMAGE, FRAME_SITE, PERSON, RADAR, VEHICLE, new_observation,
)
from services.api.fusion import FusionService


def cam_obs(ts=1000.0, sid="CAM-03", **over):
    o = new_observation(CAMERA, sid, "SITE-DXB-01", ts, 640.0, 712.0,
                        frame=FRAME_IMAGE, object_class=PERSON, confidence=0.87)
    o.update(over)
    return o


def radar_obs(ts=1000.0, sid="RAD-01", **over):
    o = new_observation(RADAR, sid, "SITE-DXB-01", ts, 12.5, 3.25,
                        frame=FRAME_SITE, object_class=PERSON, confidence=0.9)
    o.update(over)
    return o


class TestIngest(unittest.TestCase):
    def setUp(self):
        self.svc = FusionService()

    def test_valid_observation_accepted(self):
        code, body = self.svc.ingest_observation(cam_obs())
        self.assertEqual(code, 202)
        self.assertTrue(body["accepted"])
        self.assertIn("observation_id", body)

    def test_malformed_observation_is_422_with_reasons(self):
        code, body = self.svc.ingest_observation(cam_obs(confidence=9.0))
        self.assertEqual(code, 422)
        self.assertFalse(body["accepted"])
        self.assertTrue(body["errors"])

    def test_rejection_does_not_count_as_accepted(self):
        self.svc.ingest_observation(cam_obs())
        self.svc.ingest_observation(cam_obs(confidence=9.0))
        snap = self.svc.snapshot(now=1000.0)
        self.assertEqual(snap["accepted"], 1)
        self.assertEqual(snap["rejected"], 1)

    def test_rejection_is_tallied_against_a_known_source(self):
        self.svc.ingest_observation(cam_obs())
        self.svc.ingest_observation(cam_obs(confidence=9.0))
        row = self.svc.sources(now=1000.0)[0]
        self.assertEqual(row["accepted"], 1)
        self.assertEqual(row["rejected"], 1)

    def test_rejection_from_an_unknown_source_does_not_create_a_row(self):
        # A payload so malformed it never named a source must not conjure one;
        # a phantom source row is worse than an untallied rejection.
        self.svc.ingest_observation({"source_id": "GHOST"})
        self.assertEqual(self.svc.sources(), [])
        self.assertEqual(self.svc.snapshot()["rejected"], 1)

    def test_radar_is_reported_as_not_appearance_capable(self):
        _, body = self.svc.ingest_observation(radar_obs())
        self.assertFalse(body["appearance_capable"])
        _, body = self.svc.ingest_observation(cam_obs())
        self.assertTrue(body["appearance_capable"])

    def test_nothing_is_fusable_yet_and_says_why(self):
        _, body = self.svc.ingest_observation(radar_obs())
        self.assertFalse(body["fusable"])
        self.assertIn("calibration", body["fusable_reason"])


class TestSourceAccounting(unittest.TestCase):
    def setUp(self):
        self.svc = FusionService()

    def test_sources_are_tracked_separately(self):
        self.svc.ingest_observation(cam_obs(sid="CAM-03"))
        self.svc.ingest_observation(cam_obs(sid="CAM-04"))
        self.svc.ingest_observation(radar_obs(sid="RAD-01"))
        self.assertEqual(self.svc.snapshot()["source_count"], 3)

    def test_first_and_last_seen_track_the_window(self):
        self.svc.ingest_observation(cam_obs(ts=1000.0))
        self.svc.ingest_observation(cam_obs(ts=1005.0))
        row = self.svc.sources(now=1005.0)[0]
        self.assertEqual(row["first_seen"], 1000.0)
        self.assertEqual(row["last_seen"], 1005.0)
        self.assertEqual(row["accepted"], 2)

    def test_out_of_order_arrival_does_not_move_last_seen_backwards(self):
        self.svc.ingest_observation(cam_obs(ts=1005.0))
        self.svc.ingest_observation(cam_obs(ts=1000.0))
        self.assertEqual(self.svc.sources(now=1005.0)[0]["last_seen"], 1005.0)

    def test_frames_and_classes_are_counted(self):
        self.svc.ingest_observation(cam_obs())
        self.svc.ingest_observation(cam_obs(object_class=VEHICLE))
        row = self.svc.sources(now=1000.0)[0]
        self.assertEqual(row["frames"], {FRAME_IMAGE: 2})
        self.assertEqual(row["classes"], {PERSON: 1, VEHICLE: 1})

    def test_one_id_two_sensor_types_is_counted_not_dropped(self):
        self.svc.ingest_observation(cam_obs(sid="DUP"))
        code, _ = self.svc.ingest_observation(radar_obs(sid="DUP"))
        self.assertEqual(code, 202)          # the payload itself is well-formed
        row = self.svc.sources(now=1000.0)[0]
        self.assertEqual(row["type_conflicts"], 1)
        self.assertEqual(row["accepted"], 2)

    def test_silence_is_reported_after_the_window(self):
        svc = FusionService(silent_after_s=60.0)
        svc.ingest_observation(cam_obs(ts=1000.0))
        self.assertFalse(svc.sources(now=1030.0)[0]["silent"])
        self.assertTrue(svc.sources(now=1100.0)[0]["silent"])
        self.assertEqual(svc.snapshot(now=1100.0)["silent_sources"], ["CAM-03"])

    def test_sources_sorted_by_most_recent_traffic(self):
        self.svc.ingest_observation(cam_obs(sid="OLD", ts=1000.0))
        self.svc.ingest_observation(cam_obs(sid="NEW", ts=2000.0))
        self.assertEqual([r["source_id"] for r in self.svc.sources(now=2000.0)],
                         ["NEW", "OLD"])

    def test_snapshot_states_fusion_is_off_and_why(self):
        snap = self.svc.snapshot()
        self.assertFalse(snap["fusion"]["geometric"])
        self.assertIn("calibration", snap["fusion"]["reason"])
        self.assertIn(CAMERA, snap["appearance_capable_types"])
        self.assertNotIn(RADAR, snap["appearance_capable_types"])


class TestRecent(unittest.TestCase):
    def test_recent_is_bounded_per_source(self):
        svc = FusionService(recent_per_source=3)
        for i in range(10):
            svc.ingest_observation(cam_obs(ts=1000.0 + i))
        recent = svc.recent(source_id="CAM-03", limit=100)
        self.assertEqual(len(recent), 3)
        self.assertEqual([o["ts"] for o in recent], [1007.0, 1008.0, 1009.0])

    def test_recent_across_sources_is_newest_first(self):
        svc = FusionService()
        svc.ingest_observation(cam_obs(sid="CAM-03", ts=1000.0))
        svc.ingest_observation(radar_obs(sid="RAD-01", ts=1002.0))
        self.assertEqual([o["ts"] for o in svc.recent(limit=5)],
                         [1002.0, 1000.0])

    def test_recent_for_an_unknown_source_is_empty(self):
        self.assertEqual(FusionService().recent(source_id="NOPE"), [])

    def test_rejected_observations_are_not_retained(self):
        svc = FusionService()
        svc.ingest_observation(cam_obs())
        svc.ingest_observation(cam_obs(confidence=9.0))
        self.assertEqual(len(svc.recent(source_id="CAM-03", limit=100)), 1)


if __name__ == "__main__":
    unittest.main()
