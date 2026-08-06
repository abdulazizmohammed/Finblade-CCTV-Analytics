"""Camera pipelines coming back after a restart.

Nothing used to restart them: the API brought up the offline monitor, the
report scheduler and the forwarder, and left every pipeline down. The dashboard
then showed a full list of cameras, all OFFLINE, with their rows intact — which
reads as "the cameras failed" rather than "nothing started them".

Opt-in via FINBLADE_AUTOSTART_CAMERAS, because spawning several detection
processes is not something a restart should do by surprise.
"""

import asyncio
import os
import unittest

os.environ.setdefault("FINBLADE_INMEMORY", "1")

try:
    from services.api import app as app_module
    HAVE_APP = True
except Exception:                                  # noqa: BLE001
    HAVE_APP = False


class FakeManager:
    """Stands in for CameraManager — records launches, spawns nothing."""

    def __init__(self, running=()):
        self.launched = []
        self._running = set(running)
        self.explode_on = set()

    def is_running(self, camera_id):
        return camera_id in self._running

    def launch(self, camera_id, source, site_id=None, stream_host="localhost"):
        if camera_id in self.explode_on:
            raise RuntimeError("no free port")
        self.launched.append((camera_id, source, site_id, stream_host))
        return {"port": 8090, "stream_url": "http://localhost:8090/stream", "pid": 1}


@unittest.skipUnless(HAVE_APP, "fastapi not available")
class TestAutostart(unittest.TestCase):
    def setUp(self):
        self.cams = []
        self.mgr = FakeManager()
        self._real_cameras = app_module.svc.cameras
        self._real_mgr = app_module.cam_mgr
        app_module.svc.cameras = lambda: list(self.cams)
        app_module.cam_mgr = self.mgr
        self.addCleanup(setattr, app_module.svc, "cameras", self._real_cameras)
        self.addCleanup(setattr, app_module, "cam_mgr", self._real_mgr)
        os.environ["FINBLADE_AUTOSTART_DELAY"] = "0"
        self.addCleanup(os.environ.pop, "FINBLADE_AUTOSTART_DELAY", None)
        self.addCleanup(os.environ.pop, "FINBLADE_AUTOSTART_CAMERAS", None)
        self.addCleanup(os.environ.pop, "FINBLADE_STREAM_HOST", None)

    def run_autostart(self):
        asyncio.run(app_module._autostart_cameras())

    def camera(self, cid, **over):
        row = {"camera_id": cid, "site_id": "SITE-1",
               "source": "rtsp://user:pass@10.0.0.1:554/s"}
        row.update(over)
        return row

    def test_off_by_default(self):
        self.cams = [self.camera("CAM-01")]
        self.run_autostart()
        self.assertEqual([], self.mgr.launched,
                         "a restart must not spawn pipelines unasked")

    def test_starts_cameras_that_have_a_source(self):
        os.environ["FINBLADE_AUTOSTART_CAMERAS"] = "1"
        self.cams = [self.camera("CAM-01"), self.camera("CAM-02")]
        self.run_autostart()
        self.assertEqual(["CAM-01", "CAM-02"], [c[0] for c in self.mgr.launched])
        self.assertEqual("SITE-1", self.mgr.launched[0][2])

    def test_accepts_the_usual_truthy_spellings(self):
        for value in ("1", "true", "TRUE", "yes", "on"):
            self.mgr.launched.clear()
            os.environ["FINBLADE_AUTOSTART_CAMERAS"] = value
            self.cams = [self.camera("CAM-01")]
            self.run_autostart()
            self.assertEqual(1, len(self.mgr.launched), value)

    def test_skips_cameras_with_no_source(self):
        """Metadata-only rows exist — registered but never given a URL."""
        os.environ["FINBLADE_AUTOSTART_CAMERAS"] = "1"
        self.cams = [self.camera("CAM-01", source=""),
                     self.camera("CAM-02", source=None),
                     self.camera("CAM-03")]
        self.run_autostart()
        self.assertEqual(["CAM-03"], [c[0] for c in self.mgr.launched])

    def test_skips_disabled_cameras(self):
        os.environ["FINBLADE_AUTOSTART_CAMERAS"] = "1"
        self.cams = [self.camera("CAM-01", enabled=False), self.camera("CAM-02")]
        self.run_autostart()
        self.assertEqual(["CAM-02"], [c[0] for c in self.mgr.launched])

    def test_does_not_double_start_something_already_running(self):
        os.environ["FINBLADE_AUTOSTART_CAMERAS"] = "1"
        self.mgr = FakeManager(running={"CAM-01"})
        app_module.cam_mgr = self.mgr
        self.cams = [self.camera("CAM-01"), self.camera("CAM-02")]
        self.run_autostart()
        self.assertEqual(["CAM-02"], [c[0] for c in self.mgr.launched])

    def test_skips_a_source_that_is_neither_a_url_nor_a_file(self):
        os.environ["FINBLADE_AUTOSTART_CAMERAS"] = "1"
        self.cams = [self.camera("CAM-01", source="/nope/missing.mp4"),
                     self.camera("CAM-02")]
        self.run_autostart()
        self.assertEqual(["CAM-02"], [c[0] for c in self.mgr.launched])

    def test_one_failure_does_not_stop_the_rest(self):
        """A port clash on camera 1 must not leave cameras 2 and 3 down."""
        os.environ["FINBLADE_AUTOSTART_CAMERAS"] = "1"
        self.mgr.explode_on = {"CAM-01"}
        self.cams = [self.camera("CAM-01"), self.camera("CAM-02"),
                     self.camera("CAM-03")]
        self.run_autostart()
        self.assertEqual(["CAM-02", "CAM-03"], [c[0] for c in self.mgr.launched])

    def test_stream_host_is_configurable(self):
        os.environ["FINBLADE_AUTOSTART_CAMERAS"] = "1"
        os.environ["FINBLADE_STREAM_HOST"] = "10.1.2.3"
        self.cams = [self.camera("CAM-01")]
        self.run_autostart()
        self.assertEqual("10.1.2.3", self.mgr.launched[0][3])


if __name__ == "__main__":
    unittest.main()
