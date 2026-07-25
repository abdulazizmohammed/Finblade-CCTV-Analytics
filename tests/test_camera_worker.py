import unittest

from services.inference.camera_worker import CameraWorker, CameraState


class SeqCapture:
    """Yields the given frames once, then reports end-of-stream (False, None)."""
    def __init__(self, frames):
        self._frames = list(frames)
        self._i = 0
        self.released = False
    def read(self):
        if self._i < len(self._frames):
            f = self._frames[self._i]; self._i += 1
            return True, f
        return False, None
    def isOpened(self):
        return True
    def release(self):
        self.released = True


class FrozenCapture:
    def read(self):
        return True, "SAME"          # identical content forever
    def isOpened(self):
        return True
    def release(self):
        pass


def worker(source="media/clip.mp4", frames=("A", "B", "C"), **kw):
    return CameraWorker(source, "CAM-T", open_capture=lambda s: SeqCapture(list(frames)), **kw)


class TestLatestFrameAndOnline(unittest.TestCase):
    def test_keeps_only_latest_and_reports_online(self):
        w = worker(frames=("A", "B", "C"))
        w._tick(0.0); w._tick(0.1); w._tick(0.2)
        frame, ts, seq = w.read_latest()
        self.assertEqual(frame, "C")           # only the latest is buffered
        self.assertEqual(seq, 3)
        self.assertEqual(ts, 0.2)
        self.assertEqual(w.state(0.2), CameraState.ONLINE)
        self.assertGreater(w.input_fps(0.2), 0)

    def test_no_frame_yet(self):
        w = worker()
        self.assertEqual(w.read_latest(), (None, None, 0))
        self.assertEqual(w.state(0.0), CameraState.OFFLINE)  # never received a frame


class TestOffline(unittest.TestCase):
    def test_offline_after_30s_without_frames(self):
        w = worker(offline_seconds=30.0)
        w._tick(0.0)                            # one valid frame at t=0
        self.assertEqual(w.state(29.0), CameraState.ONLINE)
        self.assertEqual(w.state(31.0), CameraState.OFFLINE)  # independent of detections

    def test_offline_is_independent_of_detection(self):
        # An empty scene still delivers frames -> stays ONLINE.
        w = worker(frames=("bg", "bg2", "bg3"))
        w._tick(0.0); w._tick(10.0); w._tick(20.0)
        self.assertEqual(w.state(20.0), CameraState.ONLINE)


class TestFrozen(unittest.TestCase):
    def test_frozen_frames_go_degraded(self):
        w = CameraWorker("rtsp://x", "CAM-T", frozen_seconds=6.0,
                         open_capture=lambda s: FrozenCapture())
        w._tick(0.0)
        self.assertEqual(w.state(0.0), CameraState.ONLINE)
        for t in (1.0, 3.0, 5.0):
            w._tick(t)
        self.assertEqual(w.state(5.0), CameraState.ONLINE)   # not yet 6s
        w._tick(7.0)
        self.assertTrue(w.health(7.0)["frozen"])
        self.assertEqual(w.state(7.0), CameraState.DEGRADED)


class TestReconnectAndLoop(unittest.TestCase):
    def test_rtsp_read_failure_reconnects(self):
        w = CameraWorker("rtsp://x", "CAM-T",
                         open_capture=lambda s: SeqCapture(["A"]))  # 1 frame then EOF/err
        w._tick(0.0)                            # delivers A
        w._tick(1.0)                            # read fails -> reconnect
        h = w.health(1.0)
        self.assertEqual(h["dropped_frames"], 1)
        self.assertEqual(h["reconnects"], 1)
        self.assertEqual(w.state(1.0), CameraState.RECONNECTING)

    def test_file_eof_loops_without_counting_reconnect(self):
        w = CameraWorker("media/clip.mp4", "CAM-T", loop_file=True,
                         open_capture=lambda s: SeqCapture(["A"]))
        w._tick(0.0)                            # A
        w._tick(1.0)                            # EOF -> loop (reopen), not a fault
        h = w.health(1.0)
        self.assertEqual(h["reconnects"], 0)
        self.assertEqual(h["loops"], 1)
        w._tick(2.0)                            # reopened -> delivers A again
        self.assertEqual(w.read_latest()[0], "A")


class TestSimulateFailureRestore(unittest.TestCase):
    def test_simulate_then_restore(self):
        w = worker()
        w._tick(0.0)
        self.assertEqual(w.state(0.0), CameraState.ONLINE)
        w.simulate_failure()
        w._tick(10.0)                           # delivers nothing while failed
        self.assertEqual(w.state(31.0), CameraState.OFFLINE)  # 31s since last valid
        w.restore()
        w._tick(32.0)                           # reopens + delivers
        self.assertEqual(w.state(32.0), CameraState.ONLINE)


class TestDisabled(unittest.TestCase):
    def test_disabled_state(self):
        w = worker()
        w.disable()
        self.assertEqual(w.state(0.0), CameraState.DISABLED)
        w._tick(0.0)                            # no capture while disabled
        self.assertEqual(w.read_latest(), (None, None, 0))
        w.enable()
        w._tick(1.0)
        self.assertEqual(w.state(1.0), CameraState.ONLINE)


if __name__ == "__main__":
    unittest.main()
