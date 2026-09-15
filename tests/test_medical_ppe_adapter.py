"""The medical checkpoint adapter: class names in, finblade vocabulary out.

NO WEIGHTS ARE LOADED HERE, and none exist on this machine. These test the
mapping layer, which is the part that decides what a detection MEANS — and the
part most likely to be wrong, because checkpoint authors spell their labels
however they like and the brief and the model card already disagreed about it.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from finblade.ppe import PPE_PROFILES
from services.inference.ppe_client import (CLASS_MAP, IGNORED_CLASSES,
                                           MEDICAL_CLASS_MAP,
                                           MEDICAL_IGNORED_CLASSES,
                                           PPEDetector, _canon)


class TestCanonicalisation(unittest.TestCase):
    """Matching on the raw string means a checkpoint spelling it "Safety-Vest"
    contributes nothing, and a zone requiring a vest is judged on silence."""

    def test_separators_and_case_all_fold_together(self):
        for raw in ("Surgical Gloves", "Surgical_Gloves", "surgical-gloves",
                    "SURGICAL GLOVES", "  Surgical_Gloves  "):
            self.assertEqual(_canon(raw), "surgical_gloves")

    def test_negatives_stay_distinct_from_positives(self):
        """The whole evidence model turns on this: folding No_Surgical_Cap into
        surgical_cap would turn every violation into compliance."""
        self.assertNotEqual(_canon("No_Surgical_Cap"), _canon("Surgical_Cap"))
        self.assertNotEqual(_canon("NO-Hardhat"), _canon("Hardhat"))

    def test_industrial_names_still_fold_correctly(self):
        self.assertEqual(_canon("NO-Safety Vest"), "no_safety_vest")
        self.assertEqual(_canon("Safety Vest"), "safety_vest")


class TestMedicalClassMap(unittest.TestCase):
    def test_every_medical_ppe_type_has_a_positive_class(self):
        """An item with no positive class could never be observed COMPLIANT —
        only ever accused on absence."""
        mapped = set(MEDICAL_CLASS_MAP.values())
        for item in PPE_PROFILES["medical"]:
            self.assertIn(item, mapped, "%s has no positive class" % item)

    def test_the_four_negatives_the_checkpoint_actually_publishes(self):
        """Documented deliberately: only gloves and cap have per-item negatives.
        Gown, scrubs, face shield, goggles, coverall and shoe covers can be
        judged ONLY on absence, which is why none can be better than
        'evaluation'. If a future checkpoint adds negatives, this fails and
        somebody re-reads the evidence model."""
        negatives = {v for v in MEDICAL_CLASS_MAP.values() if v.startswith("no_")}
        self.assertEqual(negatives, {"no_surgical_gloves", "no_surgical_cap",
                                     "no_facial_gear", "no_medical_attire"})

    def test_broad_negatives_are_not_mapped_onto_a_single_item(self):
        """No_Facial_Gear means no mask AND no shield AND no goggles. Mapping it
        to 'no_surgical_mask' would invent evidence the model did not give — a
        person with goggles but no mask is No_Facial_Gear to this checkpoint."""
        self.assertEqual(MEDICAL_CLASS_MAP["No_Facial_Gear"], "no_facial_gear")
        self.assertEqual(MEDICAL_CLASS_MAP["No_Medical_Attire"],
                         "no_medical_attire")
        self.assertNotIn("no_surgical_mask", MEDICAL_CLASS_MAP.values())

    def test_the_two_maps_do_not_collide(self):
        """Both detectors' outputs land in one _owned dict per person, so a
        shared key would let an industrial detection satisfy a medical
        requirement."""
        shared = set(CLASS_MAP.values()) & set(MEDICAL_CLASS_MAP.values())
        self.assertEqual(shared, {"person"})       # person is the only overlap

    def test_goggles_appear_in_both_checkpoints_but_only_medical_maps_them(self):
        """The industrial checkpoint HAS a Goggles class and we ignore it —
        deliberately, because no industrial zone can require goggles."""
        self.assertIn("Goggles", IGNORED_CLASSES)
        self.assertEqual(MEDICAL_CLASS_MAP["Goggles"], "goggles")


class TestDetectorUsesItsOwnVocabulary(unittest.TestCase):
    def test_the_default_is_still_the_industrial_map(self):
        """Backward compatibility: every existing call site passes no map."""
        d = PPEDetector("CAM-1")
        self.assertEqual(d.class_map, CLASS_MAP)
        self.assertEqual(d.ignored_classes, IGNORED_CLASSES)
        self.assertEqual(d.profile, "industrial")

    def test_a_medical_detector_speaks_the_medical_map(self):
        d = PPEDetector("CAM-1", class_map=MEDICAL_CLASS_MAP,
                        ignored_classes=MEDICAL_IGNORED_CLASSES,
                        profile="medical")
        self.assertEqual(d.profile, "medical")
        self.assertIn("Surgical_Gloves", d.class_map)
        self.assertNotIn("Hardhat", d.class_map)

    def test_lookup_survives_a_checkpoint_that_spells_it_differently(self):
        """The model card says underscores, the brief said spaces. Neither is
        trusted: whatever the weights say must resolve."""
        d = PPEDetector("CAM-1", class_map=MEDICAL_CLASS_MAP, profile="medical")
        for spelling in ("Surgical_Gloves", "Surgical Gloves",
                         "surgical-gloves", "SURGICAL_GLOVES"):
            self.assertEqual(d._canon_map.get(_canon(spelling)),
                             "surgical_gloves")

    def test_an_unknown_class_maps_to_nothing(self):
        """An unmapped class must never become evidence."""
        d = PPEDetector("CAM-1", class_map=MEDICAL_CLASS_MAP, profile="medical")
        self.assertIsNone(d._canon_map.get(_canon("Lab_Coat")))
        self.assertIsNone(d._canon_map.get(_canon("Hardhat")))

    def test_a_detector_with_no_weights_is_disabled_not_crashing(self):
        """The weights are absent and expected to stay absent for now. The
        camera must keep running every other rule."""
        d = PPEDetector("CAM-1", weights="models/definitely_not_here.pt",
                        enabled=True, class_map=MEDICAL_CLASS_MAP,
                        profile="medical")
        self.assertFalse(d.load())
        self.assertFalse(d.enabled)
        self.assertIn("unavailable", d.status)
        self.assertEqual(d.detect(None, 0.0), [])
        self.assertFalse(d.due(1000.0))

    def test_a_disabled_detector_does_no_work(self):
        d = PPEDetector("CAM-1", enabled=False)
        self.assertFalse(d.load())
        self.assertEqual(d.status, "disabled")
        self.assertEqual(d.detect(None, 0.0), [])


if __name__ == "__main__":
    unittest.main()
