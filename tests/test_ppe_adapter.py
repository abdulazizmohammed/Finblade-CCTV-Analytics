"""PPE model adapter (cases U-X): normalisation and failure isolation.

NO WEIGHTS ARE LOADED AND NOTHING IS DOWNLOADED. The adapter is driven with a
fake model object, which is what proves the dependency isolation is real: if
any Ultralytics type were required to exercise this code, these tests could not
exist.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.inference.ppe_client import CLASS_MAP, IGNORED_CLASSES, PPEDetector


class _Boxes:
    """Duck-typed stand-in for an Ultralytics Boxes object."""

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


class _L(list):
    def tolist(self):
        return list(self)


class _Res:
    def __init__(self, rows):
        self.boxes = _Boxes(rows)


class _FakeModel:
    """Counts loads and predicts, and can be told to explode."""

    def __init__(self, names, rows=(), raises=False):
        self.names = dict(names)
        self._rows = list(rows)
        self._raises = raises
        self.predict_calls = 0

    def to(self, _device):
        return self

    def predict(self, *_a, **_kw):
        self.predict_calls += 1
        if self._raises:
            raise RuntimeError("CUDA out of memory (simulated)")
        return [_Res(self._rows)]


NAMES = {0: "Fall-Detected", 1: "Gloves", 2: "Goggles", 3: "Hardhat",
         4: "Mask", 5: "NO-Gloves", 6: "NO-Goggles", 7: "NO-Hardhat",
         8: "NO-Mask", 9: "NO-Safety Vest", 10: "No_Harness", 11: "Person",
         12: "Safety Vest"}


def detector(model, **kw):
    d = PPEDetector("CAM-01", enabled=True, **kw)
    d._model = model
    d._names = dict(model.names)
    d.status = "ready"
    return d


class TestLoadOnce(unittest.TestCase):
    def test_U_the_model_is_loaded_once_per_process(self):
        """A retrying caller must not put a second copy on the GPU."""
        d = detector(_FakeModel(NAMES))
        d.stats["loads"] = 1
        for _ in range(5):
            self.assertTrue(d.load())
        self.assertEqual(d.stats["loads"], 1)

    def test_missing_weights_disable_rather_than_raise(self):
        d = PPEDetector("CAM-01", weights="models/nope.pt", enabled=True)
        self.assertFalse(d.load())
        self.assertFalse(d.enabled)
        self.assertIn("FileNotFoundError", d.status)
        self.assertFalse(d.ready)

    def test_disabled_never_loads(self):
        d = PPEDetector("CAM-01", enabled=False)
        self.assertFalse(d.load())
        self.assertEqual(d.status, "disabled")


class TestNormalisation(unittest.TestCase):
    def test_V_results_normalise_to_plain_dicts(self):
        rows = [([10.0, 20.0, 30.0, 40.0], 3, 0.91),     # Hardhat
                ([50.0, 60.0, 70.0, 80.0], 9, 0.55)]     # NO-Safety Vest
        d = detector(_FakeModel(NAMES, rows))
        out = d.detect(object(), now=123.0)
        self.assertEqual(len(out), 2)
        for det in out:
            # The original four keys are the contract every consumer reads;
            # class_id / raw_class / is_violation were added for the lab
            # checkpoint's journal and compliance logic and ride alongside.
            self.assertLessEqual({"class_name", "confidence", "bbox", "ts"},
                                 set(det))
            self.assertIsInstance(det["class_name"], str)
            self.assertIsInstance(det["confidence"], float)
            self.assertIsInstance(det["bbox"], tuple)
            self.assertEqual(len(det["bbox"]), 4)
            self.assertEqual(det["ts"], 123.0)
        self.assertEqual(out[0]["class_name"], "hardhat")
        self.assertEqual(out[1]["class_name"], "no_safety_vest")
        self.assertEqual((out[0]["class_id"], out[0]["raw_class"]), (3, "Hardhat"))
        self.assertFalse(out[0]["is_violation"])
        self.assertTrue(out[1]["is_violation"])

    def test_V2_no_ultralytics_object_leaks_into_the_output(self):
        rows = [([1.0, 2.0, 3.0, 4.0], 3, 0.9)]
        out = detector(_FakeModel(NAMES, rows)).detect(object(), 1.0)
        for det in out:
            for v in det.values():
                self.assertIsInstance(v, (str, float, int, bool, tuple))
                self.assertNotIn("ultralytics", type(v).__module__)

    def test_W_classes_outside_phase_3_are_ignored(self):
        rows = [([1.0, 1.0, 2.0, 2.0], 0, 0.9),    # Fall-Detected
                ([1.0, 1.0, 2.0, 2.0], 1, 0.9),    # Gloves
                ([1.0, 1.0, 2.0, 2.0], 10, 0.9),   # No_Harness
                ([1.0, 1.0, 2.0, 2.0], 3, 0.9)]    # Hardhat  <- the only one kept
        d = detector(_FakeModel(NAMES, rows))
        out = d.detect(object(), 1.0)
        self.assertEqual([o["class_name"] for o in out], ["hardhat"])
        self.assertEqual(d.stats["ignored"], 3)

    def test_W2_an_unknown_class_id_is_dropped_not_guessed(self):
        d = detector(_FakeModel(NAMES, [([1.0, 1.0, 2.0, 2.0], 99, 0.9)]))
        self.assertEqual(d.detect(object(), 1.0), [])

    def test_the_ignore_list_and_the_map_do_not_overlap(self):
        self.assertEqual(set(CLASS_MAP) & IGNORED_CLASSES, set())

    def test_every_phase3_class_is_mapped(self):
        for required in ("Hardhat", "NO-Hardhat", "Safety Vest",
                         "NO-Safety Vest", "Mask", "NO-Mask", "Person"):
            self.assertIn(required, CLASS_MAP)


class TestFailureIsolation(unittest.TestCase):
    def test_X_an_inference_exception_does_not_escape(self):
        """The worker runs detection, tracking, zones and every other rule. A
        PPE model that throws must cost the PPE verdict, not the camera."""
        d = detector(_FakeModel(NAMES, raises=True))
        self.assertEqual(d.detect(object(), 1.0), [])
        self.assertEqual(d.stats["errors"], 1)
        # ...and it keeps trying rather than latching off.
        self.assertEqual(d.detect(object(), 2.0), [])
        self.assertEqual(d.stats["errors"], 2)
        self.assertTrue(d.ready)

    def test_a_dead_detector_returns_nothing(self):
        d = PPEDetector("CAM-01", enabled=False)
        self.assertEqual(d.detect(object(), 1.0), [])

    def test_empty_results_are_handled(self):
        d = detector(_FakeModel(NAMES, []))
        self.assertEqual(d.detect(object(), 1.0), [])


class TestCadence(unittest.TestCase):
    def test_it_only_runs_on_the_configured_interval(self):
        d = detector(_FakeModel(NAMES), interval_s=0.5)
        self.assertTrue(d.due(100.0))
        d.detect(object(), 100.0)
        self.assertFalse(d.due(100.2))
        self.assertTrue(d.due(100.6))

    def test_stale_detections_are_not_reused_for_the_overlay(self):
        d = detector(_FakeModel(NAMES, [([1.0, 1.0, 2.0, 2.0], 3, 0.9)]))
        d.detect(object(), 100.0)
        self.assertEqual(len(d.detections_for(100.3)), 1)
        self.assertEqual(d.detections_for(102.0), [],
                         "a PPE box left on screen after the model stopped "
                         "seeing it misrepresents the frame")


class TestNoNetwork(unittest.TestCase):
    def test_importing_the_adapter_downloads_nothing(self):
        """Guards the 'no checkpoint download during automated tests' rule."""
        import subprocess
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        code = ("import sys;sys.path.insert(0, %r);"
                "import services.inference.ppe_client as p;"
                "print('ultralytics' in sys.modules, 'torch' in sys.modules)" % repo)
        out = subprocess.run([sys.executable, "-c", code],
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "False False")


if __name__ == "__main__":
    unittest.main()
