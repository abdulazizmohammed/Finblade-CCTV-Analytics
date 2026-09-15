"""A zone edit must reach the running pipeline, not just the database.

WHY THIS TEST EXISTS. Unticking "safety vest" in the zone editor saved
correctly — the API stored ["hardhat"], the dashboard showed ["hardhat"] — and
the worker went on raising safety_vest violations against real people for as
long as it kept running. Nothing errored and nothing appeared in the log.

Two independent causes, both of which had to be fixed:

  1. _zone_sig() did not include required_ppe, so a PPE-only edit produced an
     identical signature and the hot-reload never ran at all.
  2. The hot-reload rebuilt restricted_zone_ids and loiter_zone from the new
     zones but not the PPE requirement map, which was built once at startup.

And a third consequence once those were fixed: PPETracker holds a verdict per
(track, ppe_type) that deliberately outlives a frame, so dropping a requirement
left stale NONCOMPLIANT verdicts that zone_summary kept counting — the zone card
and the alert feed disagreeing about the same instant.

The signature test is written against the ZONE FIELD SET rather than a list of
names, so a field added to Zone tomorrow fails here until someone decides
whether a live edit to it should reload. That is the question that was never
asked for required_ppe.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from finblade.ppe import NONCOMPLIANT, PPETracker, PPEThresholds
from services.inference.run_cpu import _zone_sig


def _zone(**over):
    z = {"zone_id": "Z1", "zone_type": "MONITORED", "restricted": False,
         "normalized_polygon": [[0, 0], [1, 0], [1, 1]],
         "capacity_max": 10, "area_sqm": 20.0,
         "warning_density": 2.0, "critical_density": 4.0,
         "loitering_threshold_sec": 30.0, "enabled": True,
         "physical_area_id": None, "required_ppe": ["hardhat", "safety_vest"]}
    z.update(over)
    return z


class TestTheSignatureSeesEveryEdit(unittest.TestCase):
    def test_dropping_a_ppe_requirement_changes_the_signature(self):
        """THE ONE THAT WAS BROKEN. Same signature = no reload = the worker
        keeps accusing people of an item nobody asked for."""
        before = _zone_sig([_zone()])
        after = _zone_sig([_zone(required_ppe=["hardhat"])])
        self.assertNotEqual(before, after)

    def test_adding_a_ppe_requirement_changes_the_signature(self):
        self.assertNotEqual(_zone_sig([_zone(required_ppe=[])]),
                            _zone_sig([_zone(required_ppe=["mask"])]))

    def test_clearing_every_requirement_changes_the_signature(self):
        """[] is a real edit — it means "stop judging PPE here" — and must not
        look the same as the previous list."""
        self.assertNotEqual(_zone_sig([_zone()]),
                            _zone_sig([_zone(required_ppe=[])]))

    def test_an_unrelated_edit_still_changes_it(self):
        self.assertNotEqual(_zone_sig([_zone()]), _zone_sig([_zone(capacity_max=11)]))

    def test_no_edit_leaves_it_alone(self):
        """It is checked every 4s; a signature that changed on its own would
        rebuild the zone set forever."""
        self.assertEqual(_zone_sig([_zone()]), _zone_sig([_zone()]))

    def test_every_editable_zone_field_is_covered(self):
        """Generic guard. Each field the editor can change must alter the
        signature, or an edit to it silently never reaches the pipeline.

        Fields excluded here are cosmetic or server-owned, and each is named
        with the reason — an empty exclusion list would be a lie, and a silent
        one would let the next field slip through the way required_ppe did.
        """
        cosmetic = {
            "zone_name",        # label only; nothing in the pipeline reads it
            "colour",           # overlay tint, redrawn from cfg every frame
            "adjacency_list",   # consumed by flow rules from their own config
            "updated_at",       # set by the store, not by an operator
        }
        base = _zone()
        unseen = []
        for field, value in base.items():
            if field in cosmetic or field == "zone_id":
                continue
            changed = {"normalized_polygon": [[0, 0], [1, 0], [1, 0.5]],
                       "zone_type": "RESTRICTED", "restricted": True,
                       "enabled": False, "physical_area_id": "AREA-9",
                       "required_ppe": ["mask"]}.get(field)
            if changed is None:
                changed = (value or 0) + 1 if isinstance(value, (int, float)) else "x"
            if _zone_sig([base]) == _zone_sig([_zone(**{field: changed})]):
                unseen.append(field)
        self.assertEqual(unseen, [], "editable fields invisible to the hot-reload "
                                     "signature: %s" % unseen)


class TestStaleVerdictsAreDropped(unittest.TestCase):
    def _tracker_with_both_violated(self):
        t = PPETracker("CAM-1", PPEThresholds(entry_grace_s=0.0,
                                              violation_confirm_s=1.0,
                                              recovery_confirm_s=1.0))
        for item in ("hardhat", "safety_vest"):
            for _ in range(6):
                t.observe(7, item, "absent", 0.9, 0.0, 1.0)
        return t

    def test_both_start_non_compliant(self):
        t = self._tracker_with_both_violated()
        self.assertEqual(t.verdict(7, "hardhat"), NONCOMPLIANT)
        self.assertEqual(t.verdict(7, "safety_vest"), NONCOMPLIANT)

    def test_dropping_vest_clears_its_verdict_and_leaves_hardhat(self):
        t = self._tracker_with_both_violated()
        self.assertEqual(t.retain_types({"hardhat"}), 1)
        self.assertEqual(t.verdict(7, "safety_vest"), "UNKNOWN")
        self.assertEqual(t.verdict(7, "hardhat"), NONCOMPLIANT)

    def test_the_zone_card_stops_reporting_the_dropped_item(self):
        """The actual symptom: the card kept showing a vest violation after the
        requirement was removed, because zone_summary scans every state held for
        the track rather than only the required ones."""
        t = self._tracker_with_both_violated()
        before = t.zone_summary([(7, "Z1", True)])["Z1"]
        self.assertIn("safety_vest", before["violations"])

        t.retain_types({"hardhat"})
        after = t.zone_summary([(7, "Z1", True)])["Z1"]
        self.assertNotIn("safety_vest", after["violations"])
        self.assertEqual(after["violations"], {"hardhat": 1})
        self.assertEqual(after["non_compliant"], 1)

    def test_clearing_every_requirement_makes_the_zone_compliant_not_empty(self):
        """With nothing required, everyone present is compliant — they must not
        vanish from the counts, because the payload promises
        compliant + non_compliant + not_assessable == occupancy."""
        t = self._tracker_with_both_violated()
        t.retain_types(set())
        z = t.zone_summary([(7, "Z1", True)])["Z1"]
        self.assertEqual(
            z["compliant"] + z["non_compliant"] + z["not_assessable"], 1)
        self.assertEqual(z["violations"], {})

    def test_retaining_everything_drops_nothing(self):
        t = self._tracker_with_both_violated()
        self.assertEqual(t.retain_types({"hardhat", "safety_vest"}), 0)
        self.assertEqual(t.verdict(7, "safety_vest"), NONCOMPLIANT)


if __name__ == "__main__":
    unittest.main()
