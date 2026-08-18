"""Recording what the matcher decided, so tuning stops costing a building walk.

Embeddings are never persisted, so the matcher cannot be replayed offline and
every threshold question costs a walk. The journal records the DECISIONS —
scores and verdicts, never vectors — which is enough to replay the threshold
and margin rules exactly, because those rules only compare scores to each other
and to a constant.

Two properties matter more than the feature itself: it must record nothing that
could describe a person, and it must never be able to break a resolve.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from finblade.appearance import TrackFeatureBank
from finblade.globalid import GlobalIdentityRegistry
from finblade.reid_journal import DecisionJournal
from finblade.topology import CameraTopology
from scripts.reid_replay import decide


def bank(*vs):
    b = TrackFeatureBank(capacity=5)
    for v in vs:
        b.add(list(v))
    return b


class TestItIsOffUntilAskedFor(unittest.TestCase):
    def test_no_path_means_disabled(self):
        """A system that quietly records every person it sees is a different
        product from one that does not. This is a diagnostic, not telemetry."""
        os.environ.pop("FINBLADE_REID_JOURNAL", None)
        j = DecisionJournal()
        self.assertFalse(j.enabled)
        self.assertFalse(j.record({"a": 1}))
        self.assertEqual(0, j.stats["entries"])

    def test_the_env_var_turns_it_on(self):
        path = os.path.join(tempfile.mkdtemp(), "j.jsonl")
        os.environ["FINBLADE_REID_JOURNAL"] = path
        try:
            self.assertTrue(DecisionJournal().enabled)
        finally:
            os.environ.pop("FINBLADE_REID_JOURNAL", None)


class TestItCannotBreakAResolve(unittest.TestCase):
    """The caller is in the middle of answering "who is this?". A full disk is
    a reason to stop journalling, not a reason to stop identifying people."""

    def test_an_unwritable_path_is_survived_and_counted(self):
        j = DecisionJournal(path="/proc/definitely/not/writable/j.jsonl")
        self.assertFalse(j.record({"a": 1}))
        self.assertEqual(1, j.stats["errors"])

    def test_unserialisable_content_is_survived(self):
        j = DecisionJournal(path=os.path.join(tempfile.mkdtemp(), "j.jsonl"))
        self.assertTrue(j.record({"obj": object()}), "default=str should cope")

    def test_the_file_is_bounded(self):
        """Left switched on for a week, it must not fill the disk."""
        j = DecisionJournal(path=os.path.join(tempfile.mkdtemp(), "j.jsonl"),
                            max_bytes=200)
        written = sum(1 for _ in range(50) if j.record({"padding": "x" * 40}))
        self.assertGreater(j.stats["dropped_full"], 0)
        self.assertLess(written, 50)
        self.assertLessEqual(os.path.getsize(j.path), 200)


class TestItRecordsNothingThatDescribesAPerson(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "j.jsonl")
        self.j = DecisionJournal(path=self.path)
        self.r = GlobalIdentityRegistry(
            topology=CameraTopology(transits={("CAM-01", "CAM-02"): (10.0, 150.0)},
                                    allow_unknown_pairs=False))

    def entry(self):
        self.r.resolve("CAM-01", 1, bank((1.0, 0.0), (1.0, 0.01)), now=1000.0)
        self.r.release("CAM-01", 1)
        res = self.r.resolve("CAM-02", 7, bank((1.0, 0.02), (1.0, 0.03)),
                             now=1060.0)
        return res.journal_entry("CAM-02", 7, 1060.0, zone_id="ZONE-C",
                                 bank_size=2)

    def test_no_vector_reaches_the_record(self):
        blob = json.dumps(self.entry())
        for banned in ("embedding", "vector", "bank_vectors", "person_ref"):
            self.assertNotIn(banned, blob)

    def test_every_number_in_it_is_a_score_or_a_time(self):
        e = self.entry()
        for c in e["scored"]:
            self.assertLessEqual(abs(float(c["score"])), 1.0)
            self.assertIn("dt", c)
            self.assertIn("from", c)

    def test_the_wire_format_did_not_grow(self):
        """as_dict() goes back to a camera worker on EVERY resolve. The
        diagnosis belongs in the journal, not in that reply."""
        self.r.resolve("CAM-01", 1, bank((1.0, 0.0), (1.0, 0.01)), now=1000.0)
        res = self.r.resolve("CAM-01", 2, bank((0.0, 1.0), (0.0, 1.0)), now=1001.0)
        self.assertEqual(
            {"global_ref", "matched", "score", "runner_up", "reason",
             "candidates"},
            set(res.as_dict()))


class TestItRecordsWhatReplayNeeds(unittest.TestCase):
    def test_every_scored_candidate_is_kept_not_just_the_winner(self):
        """Raising a threshold changes WHICH candidates cross it. Knowing only
        the winner's score cannot tell you what else would have crossed."""
        r = GlobalIdentityRegistry(topology=CameraTopology(allow_unknown_pairs=True))
        r.resolve("CAM-01", 1, bank((1.0, 0.0), (1.0, 0.0)), now=1000.0)
        r.resolve("CAM-01", 2, bank((0.9, 0.2), (0.9, 0.2)), now=1000.0)
        for tid in (1, 2):
            r.release("CAM-01", tid)
        res = r.resolve("CAM-02", 9, bank((1.0, 0.05), (1.0, 0.05)), now=1100.0)
        self.assertEqual(2, len(res.scored), "both candidates must be recorded")
        self.assertGreaterEqual(res.scored[0]["score"], res.scored[1]["score"])

    def test_the_physics_verdict_is_kept(self):
        """So replay can tell a surveyed hop from a guessed one without
        re-reading the topology that was live at the time."""
        r = GlobalIdentityRegistry(
            topology=CameraTopology(transits={("CAM-01", "CAM-02"): (10.0, 150.0)},
                                    allow_unknown_pairs=False))
        r.resolve("CAM-01", 1, bank((1.0, 0.0), (1.0, 0.0)), now=1000.0)
        r.release("CAM-01", 1)
        res = r.resolve("CAM-02", 9, bank((1.0, 0.0), (1.0, 0.0)), now=1060.0)
        self.assertEqual("transit_ok", res.scored[0]["gate"])
        self.assertEqual(60.0, res.scored[0]["dt"])


class TestReplayReproducesTheRules(unittest.TestCase):
    """decide() must mirror resolve()'s gate 3 exactly, or every conclusion
    drawn from a replay is about a matcher that does not exist."""

    def entry(self, *scores):
        return {"scored": [{"ref": "gp_%d" % i, "score": s}
                           for i, s in enumerate(scores)]}

    def test_a_clear_winner_matches(self):
        v, _r, _b, _u = decide(self.entry(0.90, 0.60), 0.70, 0.06, 0.70)
        self.assertEqual("appearance_match", v)

    def test_a_thin_gap_is_ambiguous(self):
        v, _r, _b, _u = decide(self.entry(0.90, 0.88), 0.70, 0.06, 0.70)
        self.assertEqual("ambiguous_margin", v)

    def test_under_the_threshold_is_refused(self):
        v, _r, _b, _u = decide(self.entry(0.50, 0.10), 0.70, 0.06, 0.70)
        self.assertEqual("below_threshold", v)

    def test_a_lone_candidate_skips_the_margin(self):
        """Matches resolve(): with one candidate there is no runner-up to beat."""
        v, _r, _b, _u = decide(self.entry(0.72), 0.70, 0.06, 0.70)
        self.assertEqual("appearance_match", v)

    def test_the_lone_floor_is_what_the_sweep_varies(self):
        """The change being evaluated: physics left one candidate, so appearance
        is corroboration rather than the deciding evidence."""
        e = self.entry(0.60)
        self.assertEqual("below_threshold", decide(e, 0.70, 0.06, 0.70)[0])
        self.assertEqual("appearance_match", decide(e, 0.70, 0.06, 0.55)[0])

    def test_the_lone_floor_does_not_leak_into_crowded_decisions(self):
        """Leniency is justified by physics having narrowed the field. With two
        candidates it has not, and the normal threshold must still apply."""
        e = self.entry(0.60, 0.30)
        self.assertEqual("below_threshold", decide(e, 0.70, 0.06, 0.40)[0])

    def test_no_candidates_is_not_a_refusal(self):
        self.assertEqual("no_candidates", decide({"scored": []}, 0.7, 0.06, 0.7)[0])


if __name__ == "__main__":
    unittest.main()
