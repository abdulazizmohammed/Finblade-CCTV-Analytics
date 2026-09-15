"""The FinBlade lab PPE checkpoint (models/ppe_yolo11s_best.pt) end to end.

TWO HALVES. The first needs no weights and proves the parts that decide what a
detection MEANS: the class order, the worn/missing pairing, the vocabulary it
lands on, the visible-threshold / journal-floor split, the item-level serving
guard and the profile-scoped absence weight. Those are where a mistake accuses
a real person, and they must be provable on a machine with no model.

The second half loads the real checkpoint and runs it on the dataset's own
test images. It skips, loudly, when either is absent — the weights are
gitignored and the images live outside the repo — and it asserts only what an
eyeless test can: the names in the weights match PPE_CLASSES, inference runs,
the output is well-formed and the journal is written. It does NOT assert the
boxes are right; nothing here can see.
"""
import json
import os
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from finblade.ppe import (NONCOMPLIANT, PPE_PROFILES, PPETracker,
                          PPEThresholds, UNKNOWN, evidence_for, profile_of)
from services.inference.ppe_client import (DEFAULT_MEDICAL_CHECKPOINT,
                                           LAB_CLASS_MAP, MEDICAL_CHECKPOINTS,
                                           PPE_CLASSES, PPE_VIOLATION_OFFSET,
                                           PPEDetector)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEIGHTS = os.path.join(REPO, "models", "ppe_yolo11s_best.pt")
SAMPLES = os.environ.get("FINBLADE_PPE_SAMPLES",
                         "/mnt/c/Users/ICSADMIN/ppe-dataset/test/images")

# The class map as the training brief states it. Duplicated here on purpose:
# the module constant is what the code trusts, this is what the human wrote,
# and the test is the only place the two are compared.
BRIEF = {0: "Gloves", 1: "Goggles", 2: "Haircap", 3: "Labcoat", 4: "Mask",
         5: "No Gloves", 6: "No Goggles", 7: "No Haircap", 8: "No Labcoat",
         9: "No Mask"}


# --- fake model plumbing (mirrors tests/test_ppe_adapter.py) -----------------
class _L(list):
    def tolist(self):
        return list(self)


class _Boxes:
    def __init__(self, rows):
        self._rows = rows

    def __len__(self):
        return len(self._rows)

    @property
    def xyxy(self):
        return _L([r[0] for r in self._rows])

    @property
    def cls(self):
        return _L([r[1] for r in self._rows])

    @property
    def conf(self):
        return _L([r[2] for r in self._rows])


class _Res:
    def __init__(self, rows):
        self.boxes = _Boxes(rows)


class _FakeModel:
    def __init__(self, names, rows=()):
        self.names = dict(names)
        self._rows = list(rows)
        self.predict_kwargs = None

    def to(self, _device):
        return self

    def predict(self, *_a, **kw):
        self.predict_kwargs = kw
        return [_Res(self._rows)]


class _fake_ultralytics:
    """Context manager that makes `from ultralytics import YOLO` return the
    given model, so load() can be driven end to end without weights. The
    weights path only has to EXIST; this file will do."""

    def __init__(self, model):
        self.model = model
        self._saved = None

    def __enter__(self):
        self._saved = sys.modules.get("ultralytics")
        mod = types.ModuleType("ultralytics")
        mod.YOLO = lambda _path: self.model
        sys.modules["ultralytics"] = mod
        return self

    def __exit__(self, *_exc):
        if self._saved is None:
            del sys.modules["ultralytics"]
        else:
            sys.modules["ultralytics"] = self._saved
        return False


def _lab_detector(rows=(), names=None, **kw):
    """A loaded lab detector over a fake model, with the real load() path."""
    model = _FakeModel(PPE_CLASSES if names is None else names, rows)
    ck = MEDICAL_CHECKPOINTS[DEFAULT_MEDICAL_CHECKPOINT]
    args = dict(weights=__file__, device="cpu", enabled=True,
                class_map=ck["class_map"], ignored_classes=ck["ignored_classes"],
                expected_names=ck["expected_names"], profile="medical")
    args.update(kw)
    d = PPEDetector("CAM-LAB", **args)
    with _fake_ultralytics(model):
        loaded = d.load()
    return d, model, loaded


# --- 1. the class map ---------------------------------------------------------
class TestClassMap(unittest.TestCase):
    def test_module_constant_matches_the_brief_exactly(self):
        """Order included. Index i and i+5 are the same item worn / missing,
        so a shuffled list inverts verdicts."""
        self.assertEqual(PPE_CLASSES, BRIEF)
        self.assertEqual(list(PPE_CLASSES), list(range(10)))

    def test_every_checkpoint_class_is_mapped_and_nothing_else_is(self):
        self.assertEqual(set(LAB_CLASS_MAP), set(PPE_CLASSES.values()))

    def test_worn_and_missing_pair_up_at_offset_five(self):
        """The whole compliance model rests on this pairing: "No X" at i+5 must
        land on "no_" + wherever X at i landed."""
        for i in range(PPE_VIOLATION_OFFSET):
            worn = LAB_CLASS_MAP[PPE_CLASSES[i]]
            missing = LAB_CLASS_MAP[PPE_CLASSES[i + PPE_VIOLATION_OFFSET]]
            self.assertEqual(missing, "no_" + worn, PPE_CLASSES[i])
            self.assertFalse(worn.startswith("no_"))

    def test_every_worn_class_lands_on_a_medical_vocabulary_item(self):
        for i in range(PPE_VIOLATION_OFFSET):
            item = LAB_CLASS_MAP[PPE_CLASSES[i]]
            self.assertIn(item, PPE_PROFILES["medical"], item)
            self.assertEqual(profile_of(item), "medical")

    def test_labcoat_is_its_own_item(self):
        """Not surgical_gown. A different garment gets a different name so the
        zone editor never claims a gown is judged by a model trained on lab
        coats."""
        self.assertEqual(LAB_CLASS_MAP["Labcoat"], "lab_coat")
        self.assertEqual(LAB_CLASS_MAP["No Labcoat"], "no_lab_coat")
        self.assertNotIn("surgical_gown", LAB_CLASS_MAP.values())

    def test_the_registry_default_is_this_checkpoint(self):
        ck = MEDICAL_CHECKPOINTS[DEFAULT_MEDICAL_CHECKPOINT]
        self.assertEqual(ck["weights"], "models/ppe_yolo11s_best.pt")
        self.assertIs(ck["class_map"], LAB_CLASS_MAP)
        self.assertEqual(ck["expected_names"], PPE_CLASSES)


# --- 2. loading: the class-order assertion -----------------------------------
class TestLoadAssertsTheClassOrder(unittest.TestCase):
    def test_matching_names_load_and_publish_served_types(self):
        d, _m, loaded = _lab_detector()
        self.assertTrue(loaded)
        self.assertEqual(d.status, "ready")
        self.assertEqual(d.served_types, {"surgical_gloves", "goggles",
                                          "surgical_cap", "lab_coat",
                                          "surgical_mask"})

    def test_a_retrain_with_a_shuffled_class_list_is_refused(self):
        """Swapping 4 and 9 would make the checkpoint say "Mask" where it means
        "No Mask". Refused outright — not warned about, not adapted."""
        names = dict(PPE_CLASSES)
        names[4], names[9] = names[9], names[4]
        d, _m, loaded = _lab_detector(names=names)
        self.assertFalse(loaded)
        self.assertFalse(d.enabled)
        self.assertIn("class map does not match", d.status)
        self.assertEqual(d.detect(object(), 0.0), [])

    def test_a_checkpoint_missing_a_class_is_refused(self):
        names = dict(PPE_CLASSES)
        del names[9]
        d, _m, loaded = _lab_detector(names=names)
        self.assertFalse(loaded)
        self.assertIn("class map does not match", d.status)

    def test_without_expected_names_a_different_checkpoint_still_loads(self):
        """The older read-and-warn path is intact for checkpoints that publish
        no canonical order (the YOLO26 candidate)."""
        names = dict(PPE_CLASSES)
        del names[4], names[9]                       # no Mask, no No Mask
        d, _m, loaded = _lab_detector(names=names, expected_names=None)
        self.assertTrue(loaded)
        self.assertEqual(d.status, "ready")
        # Serving follows the POSITIVE class actually present in the weights.
        self.assertNotIn("surgical_mask", d.served_types)
        self.assertEqual(d.served_types, {"surgical_gloves", "goggles",
                                          "surgical_cap", "lab_coat"})


# --- 3. detect(): structured output, threshold split, journal ----------------
class TestDetectOutput(unittest.TestCase):
    ROWS = [([10.0, 20.0, 30.0, 40.0], 3, 0.91),     # Labcoat, visible
            ([50.0, 60.0, 70.0, 80.0], 8, 0.62),     # No Labcoat, visible
            ([90.0, 90.0, 99.0, 99.0], 9, 0.31)]     # No Mask, journal only

    def test_is_violation_follows_the_offset(self):
        d, _m, _ = _lab_detector(self.ROWS)
        out = d.detect(object(), now=5.0, frame_id=77)
        self.assertEqual([o["class_id"] for o in out], [3, 8])
        self.assertEqual([o["is_violation"] for o in out], [False, True])
        self.assertEqual([o["class_name"] for o in out],
                         ["lab_coat", "no_lab_coat"])
        self.assertEqual(out[0]["raw_class"], "Labcoat")
        for o in out:
            self.assertEqual(o["is_violation"],
                             o["class_id"] >= PPE_VIOLATION_OFFSET)

    def test_the_model_runs_at_the_journal_floor_but_returns_the_visible_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            d, m, _ = _lab_detector(self.ROWS, conf_threshold=0.5,
                                    iou_threshold=0.5, raw_log_dir=tmp,
                                    raw_log_conf=0.25)
            out = d.detect(object(), now=5.0, frame_id=77)
            self.assertEqual(m.predict_kwargs["conf"], 0.25)
            self.assertEqual(m.predict_kwargs["iou"], 0.5)
            self.assertEqual(m.predict_kwargs["imgsz"], 640)
            self.assertEqual(len(out), 2)                   # 0.31 held back
            self.assertEqual(d.stats["below_threshold"], 1)
            self.assertEqual(d.stats["detections"], 2)
            self.assertEqual(d.stats["logged"], 3)          # all three
            with open(os.path.join(tmp, "CAM-LAB.jsonl"), encoding="utf-8") as fh:
                recs = [json.loads(line) for line in fh if line.strip()]
        self.assertEqual(len(recs), 3)
        self.assertEqual([r["class_id"] for r in recs], [3, 8, 9])
        self.assertEqual([r["frame"] for r in recs], [77, 77, 77])
        self.assertEqual(recs[2]["class"], "No Mask")
        self.assertEqual(recs[2]["mapped"], "no_surgical_mask")
        self.assertAlmostEqual(recs[2]["conf"], 0.31, places=4)
        self.assertEqual(recs[0]["bbox"], [10.0, 20.0, 30.0, 40.0])

    def test_the_journal_names_no_person(self):
        """Class, confidence, box, frame, time, camera. No crop, no track id,
        no person_ref — nothing that would make the journal a record of who."""
        with tempfile.TemporaryDirectory() as tmp:
            d, _m, _ = _lab_detector(self.ROWS, raw_log_dir=tmp, raw_log_conf=0.25)
            d.detect(object(), now=5.0, frame_id=1)
            with open(d.raw_log_path, encoding="utf-8") as fh:
                rec = json.loads(fh.readline())
        self.assertEqual(set(rec), {"ts", "frame", "camera", "profile",
                                    "class_id", "class", "mapped", "conf",
                                    "bbox"})

    def test_the_journal_floor_never_sits_above_the_visible_threshold(self):
        d, _m, _ = _lab_detector(conf_threshold=0.5, raw_log_conf=0.9)
        self.assertEqual(d.raw_log_conf, 0.5)

    def test_no_journal_dir_means_no_journal(self):
        d, m, _ = _lab_detector(self.ROWS, conf_threshold=0.5)
        d.detect(object(), 1.0)
        self.assertIsNone(d.raw_log_path)
        self.assertEqual(d.stats["logged"], 0)
        # ...and then the model runs at the visible threshold, not lower.
        self.assertEqual(m.predict_kwargs["conf"], 0.5)

    def test_frame_id_is_optional_for_older_callers(self):
        d, _m, _ = _lab_detector(self.ROWS)
        out = d.detect(object(), 1.0)
        self.assertEqual(len(out), 2)            # 0.31 < the default 0.35
        self.assertEqual(d.stats["below_threshold"], 1)


# --- 4. serving: an item this checkpoint cannot see is not judged ------------
class TestItemLevelServing(unittest.TestCase):
    class _Det:
        def __init__(self, profile, enabled, served=None):
            self.profile, self.enabled = profile, enabled
            if served is not None:
                self.served_types = set(served)

    def _served(self, zones, profiles, dets):
        from services.inference.run_cpu import ppe_served
        return ppe_served(zones, profiles, dets, "CAM-1")

    LAB = {"surgical_gloves", "goggles", "surgical_cap", "lab_coat",
           "surgical_mask"}

    def test_items_the_checkpoint_lacks_are_dropped_from_the_zone(self):
        zones = {"LAB": ["surgical_mask", "shoe_covers", "lab_coat"]}
        kept, prof = self._served(zones, {"LAB": "medical"},
                                  [self._Det("medical", True, self.LAB)])
        self.assertEqual(kept, {"LAB": ["surgical_mask", "lab_coat"]})
        self.assertEqual(prof, {"LAB": "medical"})

    def test_a_zone_wanting_only_unserved_items_disappears(self):
        zones = {"LAB": ["shoe_covers", "face_shield"]}
        kept, prof = self._served(zones, {"LAB": "medical"},
                                  [self._Det("medical", True, self.LAB)])
        self.assertEqual(kept, {})
        self.assertEqual(prof, {})

    def test_a_detector_that_publishes_nothing_serves_its_whole_profile(self):
        """Older adapters and stubs: the pre-existing behaviour, unchanged."""
        zones = {"LAB": ["shoe_covers", "surgical_mask"]}
        kept, _ = self._served(zones, {"LAB": "medical"},
                               [self._Det("medical", True)])
        self.assertEqual(kept, zones)

    def test_the_industrial_zone_is_untouched_by_the_lab_checkpoint(self):
        zones = {"YARD": ["hardhat"], "LAB": ["surgical_mask", "coverall"]}
        profiles = {"YARD": "industrial", "LAB": "medical"}
        kept, _ = self._served(zones, profiles,
                               [self._Det("industrial", True),
                                self._Det("medical", True, self.LAB)])
        self.assertEqual(kept, {"YARD": ["hardhat"], "LAB": ["surgical_mask"]})


# --- 5. compliance over time: explicit negatives only, sustained -------------
class TestTemporalCompliance(unittest.TestCase):
    def _tracker(self, medical_absence=0.0):
        return PPETracker("CAM-LAB", PPEThresholds(
            entry_grace_s=0.0, violation_confirm_s=8.0,
            recovery_confirm_s=5.0, min_confidence=0.40, absence_weight=0.25,
            absence_weight_by_profile={"medical": medical_absence}))

    def test_evidence_reduces_the_lab_vocabulary(self):
        self.assertEqual(evidence_for("lab_coat", [("no_lab_coat", 0.7)]),
                         ("negative", 0.7))
        self.assertEqual(evidence_for("lab_coat", [("lab_coat", 0.8)]),
                         ("positive", 0.8))
        # A No Labcoat and a Labcoat on one person: the negative wins.
        self.assertEqual(evidence_for("lab_coat", [("lab_coat", 0.9),
                                                   ("no_lab_coat", 0.6)])[0],
                         "negative")

    def test_a_single_no_labcoat_frame_is_not_a_violation(self):
        t = self._tracker()
        t.observe(1, "lab_coat", "negative", 0.9, 0.0, 0.5)
        self.assertNotEqual(t.verdict(1, "lab_coat"), NONCOMPLIANT)

    def test_sustained_no_labcoat_is(self):
        t = self._tracker()
        for i in range(20):                       # 10s of explicit negatives
            t.observe(1, "lab_coat", "negative", 0.9, i * 0.5, 0.5)
        self.assertEqual(t.verdict(1, "lab_coat"), NONCOMPLIANT)

    def test_silence_never_convicts_a_medical_item_at_weight_zero(self):
        """The lab model's recall is 0.32 and at conf 0.5 it emitted no
        Gloves / Haircap / Mask positive on its own test images. Silence is
        what it does when it is working; it must not be evidence."""
        t = self._tracker(medical_absence=0.0)
        for i in range(600):                      # five minutes of nothing
            t.observe(1, "surgical_mask", "absent", 0.0, i * 0.5, 0.5)
        self.assertEqual(t.verdict(1, "surgical_mask"), UNKNOWN)

    def test_the_override_is_per_profile_not_global(self):
        """Industrial silence still counts, at the industrial weight."""
        t = self._tracker(medical_absence=0.0)
        for i in range(80):
            t.observe(1, "hardhat", "absent", 0.0, i * 0.5, 0.5)
        self.assertEqual(t.verdict(1, "hardhat"), NONCOMPLIANT)
        self.assertEqual(t.t.absence_weight_for("hardhat"), 0.25)
        self.assertEqual(t.t.absence_weight_for("surgical_mask"), 0.0)

    def test_no_override_means_the_global_weight(self):
        t = PPETracker("CAM", PPEThresholds(absence_weight=0.25))
        self.assertEqual(t.t.absence_weight_for("surgical_mask"), 0.25)
        self.assertEqual(t.t.absence_weight_for("not_an_item"), 0.25)

    def test_a_low_confidence_negative_is_ignored(self):
        """Below min_confidence a "No Mask" is treated as silence — and with
        the medical weight at zero, silence is nothing."""
        t = self._tracker()
        for i in range(40):
            t.observe(1, "surgical_mask", "negative", 0.30, i * 0.5, 0.5)
        self.assertEqual(t.verdict(1, "surgical_mask"), UNKNOWN)


# --- 6. the real checkpoint --------------------------------------------------
def _samples(n):
    if not os.path.isdir(SAMPLES):
        return []
    names = sorted(f for f in os.listdir(SAMPLES)
                   if f.lower().endswith((".jpg", ".jpeg", ".png")))
    return [os.path.join(SAMPLES, f) for f in names[:n]]


@unittest.skipUnless(os.path.exists(WEIGHTS),
                     "models/ppe_yolo11s_best.pt not present (gitignored; "
                     "copy it from the training output)")
class TestRealCheckpoint(unittest.TestCase):
    """CPU, at most ten images, and only claims an eyeless test can make."""

    @classmethod
    def setUpClass(cls):
        try:
            import cv2  # noqa: F401
            from ultralytics import YOLO  # noqa: F401
        except ImportError as exc:
            raise unittest.SkipTest("vision stack not installed: %s" % exc)

    def test_names_in_the_weights_match_PPE_CLASSES(self):
        from ultralytics import YOLO
        names = {int(k): str(v) for k, v in YOLO(WEIGHTS).names.items()}
        self.assertEqual(names, PPE_CLASSES)

    def test_the_detector_loads_runs_and_journals(self):
        imgs = _samples(10)
        if not imgs:
            self.skipTest("no sample images at %s (set FINBLADE_PPE_SAMPLES)"
                          % SAMPLES)
        import cv2
        ck = MEDICAL_CHECKPOINTS[DEFAULT_MEDICAL_CHECKPOINT]
        with tempfile.TemporaryDirectory() as tmp:
            d = PPEDetector("CAM-LAB", weights=WEIGHTS, device="cpu",
                            enabled=True, interval_s=0.0, conf_threshold=0.5,
                            iou_threshold=0.5, imgsz=640,
                            class_map=ck["class_map"],
                            ignored_classes=ck["ignored_classes"],
                            expected_names=ck["expected_names"],
                            profile="medical", raw_log_dir=tmp,
                            raw_log_conf=0.25)
            self.assertTrue(d.load(), d.status)
            self.assertEqual(d._names, PPE_CLASSES)
            self.assertEqual(d.served_types, {"surgical_gloves", "goggles",
                                              "surgical_cap", "lab_coat",
                                              "surgical_mask"})
            visible = []
            for i, path in enumerate(imgs):
                frame = cv2.imread(path)
                self.assertIsNotNone(frame, path)
                visible.extend(d.detect(frame, now=float(i), frame_id=i))
            self.assertEqual(d.stats["errors"], 0)
            self.assertEqual(d.stats["runs"], len(imgs))
            for det in visible:
                self.assertGreaterEqual(det["confidence"], 0.5)
                self.assertEqual(det["class_name"],
                                 LAB_CLASS_MAP[det["raw_class"]])
                self.assertEqual(det["is_violation"],
                                 det["class_id"] >= PPE_VIOLATION_OFFSET)
                self.assertEqual(len(det["bbox"]), 4)
            with open(d.raw_log_path, encoding="utf-8") as fh:
                recs = [json.loads(line) for line in fh if line.strip()]
        self.assertEqual(len(recs), d.stats["logged"])
        # A first-pass model, but one that produced boxes on 30 of these 47
        # images at 0.25 when it was integrated. A retrain that emits NOTHING
        # on its own test images at that floor is broken, not merely weak.
        self.assertGreater(len(recs), 0,
                           "no raw detections at conf 0.25 across %d test "
                           "images — is this the right checkpoint?" % len(imgs))
        for r in recs:
            self.assertIn(r["class_id"], PPE_CLASSES)
            self.assertEqual(r["class"], PPE_CLASSES[r["class_id"]])
            self.assertGreaterEqual(r["conf"], 0.25)


if __name__ == "__main__":
    unittest.main()
