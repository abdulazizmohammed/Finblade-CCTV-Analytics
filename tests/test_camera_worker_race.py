"""The health poll must not be able to kill the camera worker.

WHAT HAPPENED. CameraWorker runs a capture thread that appends every frame's
timestamp to a deque, trimming the old ones. The main loop reads the same deque
in input_fps() to report health. Nothing guarded it, so a health poll landing
mid-append raised

    RuntimeError: deque mutated during iteration

out of CameraWorker.input_fps. Nothing caught it, the worker exited with
"terminate called without an active exception", and the camera went quiet. CAM-04
died this way; CAM-05 and CAM-06 had eight and ten reconnects behind them.

The damage ran well past one camera. Every restart resets ByteTrack, so tracks
begin inside a zone rather than crossing into one — which is why four in five
arrivals carried no previous zone, why two thirds of sightings were a single
event, and why the facility roster held people who had long since left. A day
spent tuning thresholds was really measuring how often the workers fell over.

These tests drive the two threads against each other. Without the lock the race
fires within a few thousand iterations; a run that reaches the end proves
nothing on its own, which is why the first test asserts the guard exists.
"""

import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.inference.camera_worker import CameraWorker


def worker():
    # No source is opened: these tests drive the bookkeeping directly, so they
    # need neither a camera nor cv2.
    return CameraWorker(camera_id="CAM-TEST", source="rtsp://nothing/here")


class TestTheWindowIsGuarded(unittest.TestCase):
    def test_the_deque_has_its_own_lock(self):
        """Asserted directly, because a passing race test can be luck."""
        w = worker()
        self.assertIsInstance(w._fps_lock, type(threading.Lock()))

    def test_it_is_not_the_same_lock_health_already_holds(self):
        """health() takes _lock and THEN calls input_fps(). threading.Lock is
        not reentrant, so guarding the window with _lock would swap a crash for
        a hang — which is worse, because a hung worker still looks alive."""
        w = worker()
        self.assertIsNot(w._fps_lock, w._lock)

    def test_health_completes_while_the_window_is_being_written(self):
        """The exact call path that died: health() -> input_fps() -> iterate."""
        w = worker()
        stop = threading.Event()
        errors = []

        def produce():
            while not stop.is_set():
                try:
                    with w._fps_lock:
                        w._ts_window.append(time.time())
                        cut = time.time() - w._fps_window
                        while w._ts_window and w._ts_window[0] < cut:
                            w._ts_window.popleft()
                except Exception as exc:                    # noqa: BLE001
                    errors.append(exc)
                    return

        t = threading.Thread(target=produce, daemon=True)
        t.start()
        try:
            for _ in range(4000):
                try:
                    w.health()
                except Exception as exc:                    # noqa: BLE001
                    errors.append(exc)
                    break
        finally:
            stop.set()
            t.join(timeout=2)

        self.assertEqual([], errors,
                         "the health poll raised while frames were arriving")

    def test_input_fps_survives_a_hostile_writer(self):
        w = worker()
        stop = threading.Event()
        errors = []

        def churn():
            while not stop.is_set():
                with w._fps_lock:
                    w._ts_window.append(time.time())
                    if len(w._ts_window) > 200:
                        w._ts_window.popleft()

        t = threading.Thread(target=churn, daemon=True)
        t.start()
        try:
            for _ in range(6000):
                try:
                    w.input_fps()
                except Exception as exc:                    # noqa: BLE001
                    errors.append(exc)
                    break
        finally:
            stop.set()
            t.join(timeout=2)

        self.assertEqual([], errors)


class TestItStillMeasuresFps(unittest.TestCase):
    """The lock must not have turned the measurement into a constant."""

    def test_recent_frames_are_counted(self):
        w = worker()
        now = 1000.0
        with w._fps_lock:
            for i in range(30):
                w._ts_window.append(now - i * 0.1)
        got = w.input_fps(now)
        self.assertGreater(got, 0.0)

    def test_an_empty_window_is_zero_not_an_error(self):
        self.assertEqual(0.0, worker().input_fps(1000.0))

    def test_stamps_outside_the_window_do_not_count(self):
        w = worker()
        now = 1000.0
        with w._fps_lock:
            w._ts_window.append(now - 10_000.0)
        self.assertEqual(0.0, w.input_fps(now))


if __name__ == "__main__":
    unittest.main()
