"""Medical / laboratory PPE: a second vocabulary through the same rule engine.

WHAT THIS DOES AND DOES NOT COVER. There are no medical weights on this
machine and the pinned ultralytics cannot load the candidate checkpoint, so
nothing here exercises a real model. What it does exercise is everything
between the model and the alert — the vocabulary, the profile boundary, the
anatomical bands, the association refusals and the evidence model — which is
where the decisions live and where a mistake accuses a real person.

The deliberate consequence: swapping in a different medical checkpoint later is
a change to one mapping, and these tests still hold.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from finblade.geometry import ANATOMY, anatomical_region, associate_item
from finblade.ppe import (ALL_PPE_TYPES, NONCOMPLIANT, COMPLIANT,
                          PPE_PROFILES, PPE_STATUS, PPE_TYPES, PPETracker,
                          PPEThresholds, STATUS_EVALUATION,
                          STATUS_EXPERIMENTAL, normalize_profile, profile_of,
                          status_of, types_for)
from finblade.zones import (ppe_requirements, ppe_requirements_rejected,
                            zone_from_dict)


def _zone(**over):
    d = {"zone_id": "Z1", "polygon": [[0, 0], [100, 0], [100, 100], [0, 100]]}
    d.update(over)
    return zone_from_dict(d, 640, 480)


class TestVocabulary(unittest.TestCase):
    def test_industrial_vocabulary_is_unchanged(self):
        """BACKWARD COMPATIBILITY. PPE_TYPES is read by existing code and tests
        to mean 'what an industrial zone may require'. Widening it to the union
        would make a building site accept 'shoe_covers'."""
        self.assertEqual(PPE_TYPES, ("hardhat", "safety_vest", "mask"))

    def test_the_two_profiles_do_not_share_an_item(self):
        """Names are globally unique, which is what lets ONE anatomy table and
        ONE state machine serve both. An overlap would make profile_of()
        ambiguous and an item's band depend on who was asking."""
        a, b = (set(PPE_PROFILES["industrial"]), set(PPE_PROFILES["medical"]))
        self.assertEqual(a & b, set())

    def test_industrial_mask_and_surgical_mask_are_different_items(self):
        """They are different objects with different detectors. Collapsing them
        would let a dust mask satisfy a surgical mask requirement."""
        self.assertIn("mask", PPE_PROFILES["industrial"])
        self.assertIn("surgical_mask", PPE_PROFILES["medical"])
        self.assertNotEqual("mask", "surgical_mask")

    def test_profile_lookup_round_trips(self):
        for name, items in PPE_PROFILES.items():
            for item in items:
                self.assertEqual(profile_of(item), name)
        self.assertIsNone(profile_of("lab_coat"))       # not modelled

    def test_types_for_unknown_profile_is_empty_not_everything(self):
        """A typo'd profile must enforce NOTHING rather than fall back to a
        default vocabulary the operator did not choose."""
        self.assertEqual(types_for("medcal"), ())
        self.assertEqual(types_for("nonsense"), ())

    def test_absent_profile_means_industrial(self):
        self.assertEqual(normalize_profile(None), "industrial")
        self.assertEqual(normalize_profile(""), "industrial")
        self.assertEqual(normalize_profile("  "), "industrial")

    def test_profile_names_are_normalised_like_every_other_config_value(self):
        for raw in ("Medical", "MEDICAL", "  medical  "):
            self.assertEqual(normalize_profile(raw), "medical")


class TestCapabilityStatus(unittest.TestCase):
    def test_nothing_is_claimed_validated(self):
        """Nothing may be marked validated without measurements on site
        footage. Not the medical items, and not the industrial ones either —
        running for weeks is not measuring."""
        self.assertNotIn("validated", set(PPE_STATUS.values()))

    def test_gloves_are_experimental_not_evaluation(self):
        """Hands have no fixed vertical position, so gloves fall to the
        whole-body band and most detections end up unattributable. Presenting
        that as merely 'evaluation' would overstate it."""
        self.assertEqual(status_of("surgical_gloves"), STATUS_EXPERIMENTAL)

    def test_every_item_has_a_status(self):
        for item in ALL_PPE_TYPES:
            self.assertIn(status_of(item),
                          (STATUS_EVALUATION, STATUS_EXPERIMENTAL))

    def test_an_unlisted_item_is_experimental_not_assumed_fine(self):
        self.assertEqual(status_of("lab_coat"), STATUS_EXPERIMENTAL)


class TestZoneConfiguration(unittest.TestCase):
    def test_a_medical_zone_keeps_its_medical_items(self):
        z = _zone(ppe_profile="medical",
                  required_ppe=["surgical_gloves", "surgical_mask"])
        self.assertEqual(ppe_requirements(z),
                         ["surgical_gloves", "surgical_mask"])

    def test_an_industrial_zone_keeps_its_industrial_items(self):
        z = _zone(required_ppe=["hardhat", "safety_vest"])
        self.assertEqual(z.ppe_profile, "industrial")
        self.assertEqual(ppe_requirements(z), ["hardhat", "safety_vest"])

    def test_a_zone_predating_profiles_still_loads(self):
        """THE BACKWARD-COMPATIBILITY CASE. Every zone in the database was
        written before this field existed."""
        z = _zone(required_ppe=["hardhat"])
        self.assertEqual(z.ppe_profile, "industrial")
        self.assertEqual(ppe_requirements(z), ["hardhat"])

    def test_a_zone_with_no_ppe_at_all_still_loads(self):
        z = _zone()
        self.assertEqual(z.required_ppe, [])
        self.assertEqual(ppe_requirements(z), [])

    def test_an_item_from_the_wrong_profile_is_dropped_not_judged(self):
        """A medical zone asking for a hardhat would be judged by a detector
        with no hardhat class — so it would be judged on silence, and everyone
        in a pathology lab would be convicted of not wearing one."""
        z = _zone(ppe_profile="medical",
                  required_ppe=["surgical_mask", "hardhat"])
        self.assertEqual(ppe_requirements(z), ["surgical_mask"])
        self.assertEqual(ppe_requirements_rejected(z), ["hardhat"])

    def test_an_invented_item_is_dropped(self):
        z = _zone(ppe_profile="medical", required_ppe=["lab_coat"])
        self.assertEqual(ppe_requirements(z), [])
        self.assertEqual(ppe_requirements_rejected(z), ["lab_coat"])

    def test_an_unknown_profile_enforces_nothing(self):
        """Fail closed. An operator who typo'd the profile gets no enforcement
        and a rejected list to look at, not enforcement against a vocabulary
        they did not pick."""
        z = _zone(ppe_profile="medcal", required_ppe=["surgical_mask"])
        self.assertEqual(ppe_requirements(z), [])
        self.assertEqual(ppe_requirements_rejected(z), ["surgical_mask"])

    def test_medical_item_names_are_normalised_from_human_config(self):
        z = _zone(ppe_profile="Medical",
                  required_ppe=["Surgical Gloves", "SHOE-COVERS"])
        self.assertEqual(z.required_ppe, ["surgical_gloves", "shoe_covers"])
        self.assertEqual(ppe_requirements(z), ["surgical_gloves", "shoe_covers"])

    def test_the_profile_survives_serialisation(self):
        z = _zone(ppe_profile="medical", required_ppe=["goggles"])
        self.assertEqual(z.to_dict()["ppe_profile"], "medical")
        self.assertEqual(z.to_dict()["required_ppe"], ["goggles"])


class TestAnatomicalBands(unittest.TestCase):
    """Bands are REASONED / NOT YET SITE VALIDATED. These assert the ordering
    relationships that make them coherent, not the exact numbers — the numbers
    will move when someone measures them, the relationships should not."""

    PERSON = (0.0, 0.0, 40.0, 160.0)      # x1,y1,x2,y2 — 40x160px

    def _band(self, item):
        return anatomical_region(self.PERSON, item)

    def test_goggles_sit_above_the_mask_band(self):
        """Eyes are above the mouth. If goggles reached the chin, a surgical
        mask would satisfy a goggles requirement."""
        self.assertLess(self._band("goggles")[3], self._band("surgical_mask")[3])

    def test_a_face_shield_reaches_lower_than_goggles(self):
        """It hangs from a headband down to the sternum."""
        self.assertGreater(self._band("face_shield")[3],
                           self._band("goggles")[3])

    def test_a_gown_reaches_lower_than_scrubs(self):
        """Surgical gowns are mid-calf; scrubs are a torso garment."""
        self.assertGreater(self._band("surgical_gown")[3],
                           self._band("surgical_scrubs")[3])

    def test_the_cap_band_starts_at_or_above_the_head(self):
        self.assertLessEqual(self._band("surgical_cap")[1], self.PERSON[1])

    def test_shoe_covers_are_at_the_feet_and_overshoot(self):
        """A person box is routinely clipped at the ankle, so the band has to
        reach past the bottom of it."""
        top = self._band("shoe_covers")[1]
        bottom = self._band("shoe_covers")[3]
        self.assertGreater(top, self.PERSON[1] + 0.5 * 160)   # lower half
        self.assertGreater(bottom, self.PERSON[3])            # overshoots

    def test_a_cap_cannot_satisfy_a_gown_requirement(self):
        """The bands must not overlap enough for a head item to be contained in
        the torso region."""
        cap = self._band("surgical_cap")
        gown = self._band("surgical_gown")
        self.assertLess(cap[3], gown[1] + 0.5 * (gown[3] - gown[1]))

    def test_gloves_have_NO_band_and_that_is_deliberate(self):
        """Hands move through the whole box. A band would be wrong most of the
        time, and a wrong band silently DROPS correct detections — the
        dangerous direction for PPE."""
        self.assertNotIn("surgical_gloves", ANATOMY)
        band = self._band("surgical_gloves")
        self.assertEqual((band[1], band[3]), (self.PERSON[1], self.PERSON[3]))

    def test_every_medical_item_except_gloves_has_a_band(self):
        for item in PPE_PROFILES["medical"]:
            if item == "surgical_gloves":
                continue
            self.assertIn(item, ANATOMY, "%s has no anatomical band" % item)


class TestAssociation(unittest.TestCase):
    """Two people and one item: the refusals matter more than the matches."""

    LEFT = (0.0, 0.0, 40.0, 160.0)
    RIGHT = (200.0, 0.0, 240.0, 160.0)

    def _people(self):
        return {"left": self.LEFT, "right": self.RIGHT}

    def test_a_cap_goes_to_the_person_wearing_it(self):
        cap = (5.0, 0.0, 35.0, 20.0)          # on LEFT's head
        owner, _ = associate_item(cap, "surgical_cap", self._people())
        self.assertEqual(owner, "left")

    def test_a_mask_goes_to_the_person_wearing_it(self):
        mask = (205.0, 10.0, 235.0, 35.0)     # on RIGHT's face
        owner, _ = associate_item(mask, "surgical_mask", self._people())
        self.assertEqual(owner, "right")

    def test_a_gown_goes_to_the_person_wearing_it(self):
        gown = (2.0, 30.0, 38.0, 120.0)
        owner, _ = associate_item(gown, "surgical_gown", self._people())
        self.assertEqual(owner, "left")

    def test_a_shoe_cover_goes_to_the_person_standing_in_it(self):
        shoe = (205.0, 145.0, 235.0, 162.0)
        owner, _ = associate_item(shoe, "shoe_covers", self._people())
        self.assertEqual(owner, "right")

    def test_an_item_between_two_people_is_refused(self):
        """Unattributable is a valid answer. The alternative is a coin toss
        that accuses whoever sorted first."""
        orphan = (100.0, 60.0, 130.0, 90.0)   # between both
        owner, _ = associate_item(orphan, "surgical_gown", self._people())
        self.assertIsNone(owner)

    def test_overlapping_people_refuse_a_head_item(self):
        """Two techs shoulder to shoulder produce overlapping head regions and
        geometry cannot arbitrate. Refusing is correct."""
        people = {"a": (0.0, 0.0, 40.0, 160.0), "b": (8.0, 0.0, 48.0, 160.0)}
        cap = (12.0, 0.0, 36.0, 18.0)
        owner, _ = associate_item(cap, "surgical_cap", people)
        self.assertIsNone(owner)

    def test_a_glove_falls_back_to_whole_body_containment(self):
        """EXPERIMENTAL, and this test documents WHY rather than asserting it
        works: with no band, association degrades to 'inside this person',
        which is why gloves are marked experimental."""
        glove = (5.0, 70.0, 20.0, 90.0)       # at LEFT's waist
        owner, _ = associate_item(glove, "surgical_gloves", self._people())
        self.assertEqual(owner, "left")

    def test_a_glove_between_two_people_is_still_refused(self):
        """The saving grace of the fallback: the margin rule still refuses when
        it cannot tell. Experimental means unreliable, not reckless."""
        glove = (100.0, 70.0, 115.0, 90.0)
        owner, _ = associate_item(glove, "surgical_gloves", self._people())
        self.assertIsNone(owner)

    def test_an_item_on_nobody_is_refused(self):
        stray = (400.0, 400.0, 420.0, 420.0)
        owner, _ = associate_item(stray, "surgical_mask", self._people())
        self.assertIsNone(owner)


class TestRulesOverMedicalItems(unittest.TestCase):
    """The state machine is item-agnostic; these prove it stays so."""

    def _tracker(self):
        return PPETracker("CAM-LAB", PPEThresholds(entry_grace_s=0.0,
                                                   violation_confirm_s=8.0,
                                                   recovery_confirm_s=5.0,
                                                   min_confidence=0.40,
                                                   absence_weight=0.25))

    def test_one_missed_frame_is_not_a_violation(self):
        t = self._tracker()
        t.observe(1, "surgical_mask", "absent", 0.0, 0.0, 0.5)
        self.assertNotEqual(t.verdict(1, "surgical_mask"), NONCOMPLIANT)

    def test_a_sustained_explicit_negative_does_alert(self):
        t = self._tracker()
        for i in range(20):
            t.observe(1, "surgical_mask", "negative", 0.9, i * 0.5, 0.5)
        self.assertEqual(t.verdict(1, "surgical_mask"), NONCOMPLIANT)

    def test_silence_takes_four_times_as_long_as_an_explicit_negative(self):
        """absence_weight 0.25. A gown that simply is not detected is not the
        same evidence as a model asserting there is no gown."""
        neg, absent = self._tracker(), self._tracker()
        for i in range(20):
            neg.observe(1, "surgical_gown", "negative", 0.9, i * 0.5, 0.5)
            absent.observe(1, "surgical_gown", "absent", 0.0, i * 0.5, 0.5)
        self.assertEqual(neg.verdict(1, "surgical_gown"), NONCOMPLIANT)
        self.assertNotEqual(absent.verdict(1, "surgical_gown"), NONCOMPLIANT)

    def test_a_low_confidence_detection_is_ignored(self):
        t = self._tracker()
        for i in range(20):
            t.observe(1, "goggles", "positive", 0.20, i * 0.5, 0.5)
        self.assertNotEqual(t.verdict(1, "goggles"), COMPLIANT)

    def test_recovery_clears_a_confirmed_violation(self):
        t = self._tracker()
        for i in range(20):
            t.observe(1, "surgical_cap", "negative", 0.9, i * 0.5, 0.5)
        self.assertEqual(t.verdict(1, "surgical_cap"), NONCOMPLIANT)
        for i in range(20, 50):
            t.observe(1, "surgical_cap", "positive", 0.9, i * 0.5, 0.5)
        self.assertEqual(t.verdict(1, "surgical_cap"), COMPLIANT)

    def test_items_are_judged_independently(self):
        """Missing a mask must not convict the gown, and the zone card counts
        PEOPLE while violations counts ITEMS."""
        t = self._tracker()
        for i in range(20):
            t.observe(1, "surgical_mask", "negative", 0.9, i * 0.5, 0.5)
            t.observe(1, "surgical_gown", "positive", 0.9, i * 0.5, 0.5)
        self.assertEqual(t.verdict(1, "surgical_mask"), NONCOMPLIANT)
        self.assertEqual(t.verdict(1, "surgical_gown"), COMPLIANT)
        z = t.zone_summary([(1, "LAB", True)])["LAB"]
        self.assertEqual(z["non_compliant"], 1)
        self.assertEqual(z["violations"], {"surgical_mask": 1})

    def test_a_person_too_small_to_judge_is_not_assessable(self):
        t = PPETracker("CAM-LAB", PPEThresholds(min_person_height_px=120.0))
        self.assertFalse(t.assessable((0.0, 0.0, 20.0, 80.0)))
        self.assertTrue(t.assessable((0.0, 0.0, 40.0, 200.0)))


if __name__ == "__main__":
    unittest.main()
