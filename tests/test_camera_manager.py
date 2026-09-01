"""CameraManager — the supervisor for every per-camera pipeline.

It had no test of any kind, which is a poor place for a blind spot: it builds
the argv that launches a detection process from a user-supplied RTSP URL, hands
out MJPEG ports, and is what stops a deleted camera leaving an orphaned
pipeline holding a camera connection.

Subprocess launch is faked. What is under test is the logic around it — port
allocation, argv construction, and the injection guarantee — not Popen itself.
"""

import os
import unittest

os.environ.setdefault("FINBLADE_INMEMORY", "1")

from services.api.camera_manager import CameraManager, _slug


class FakeProc:
    def __init__(self, pid=4242, alive=True):
        self.pid = pid
        self._alive = alive
        self.terminated = False

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self.terminated = True
        self._alive = False

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self._alive = False


class ManagerHarness(CameraManager):
    """CameraManager with the two OS calls replaced."""

    def __init__(self, *a, **kw):
        self.launched = []           # every argv list handed to Popen
        self.pkilled = []
        super().__init__(*a, **kw)

    @staticmethod
    def _port_free(port):
        return True

    def _spawn(self, cmd, log):
        self.launched.append(list(cmd))
        return FakeProc()


def _patch(mgr):
    """Route Popen/pkill/open through the harness instead of the OS."""
    import services.api.camera_manager as cm

    class _Log:
        def close(self):
            pass

    mgr._orig = (cm.subprocess.Popen, cm.subprocess.run, cm.open
                 if hasattr(cm, "open") else None)
    cm.subprocess.Popen = lambda cmd, **kw: mgr._spawn(cmd, None)
    cm.subprocess.run = lambda cmd, **kw: mgr.pkilled.append(list(cmd))
    return _Log


class TestSlug(unittest.TestCase):
    def test_alphanumerics_and_dashes_survive(self):
        self.assertEqual(_slug("CAM-A_01"), "CAM-A_01")

    def test_path_separators_are_neutralised(self):
        # The slug names a log file; a camera id containing a slash must not be
        # able to steer that write out of the log directory.
        self.assertNotIn("/", _slug("../../etc/passwd"))
        self.assertNotIn("\\", _slug("a\\b"))

    def test_spaces_and_punctuation_become_underscores(self):
        self.assertEqual(_slug("cam 1;rm -rf"), "cam_1_rm_-rf")


class TestPortAllocation(unittest.TestCase):
    def setUp(self):
        self.mgr = ManagerHarness(base_port=8090)
        self.LogCls = _patch(self.mgr)
        import services.api.camera_manager as cm
        cm.open = lambda *a, **kw: self.LogCls()

    def tearDown(self):
        import services.api.camera_manager as cm
        cm.subprocess.Popen, cm.subprocess.run, _ = self.mgr._orig
        if hasattr(cm, "open"):
            del cm.open

    def test_first_camera_gets_the_base_port(self):
        info = self.mgr.launch("CAM-1", "rtsp://host/1")
        self.assertEqual(info["port"], 8090)

    def test_each_camera_gets_a_distinct_port(self):
        ports = {self.mgr.launch(f"CAM-{i}", "rtsp://h/x")["port"]
                 for i in range(4)}
        self.assertEqual(len(ports), 4)

    def test_a_stopped_camera_releases_its_port(self):
        first = self.mgr.launch("CAM-1", "rtsp://h/1")["port"]
        self.mgr.stop("CAM-1")
        self.assertEqual(self.mgr.launch("CAM-2", "rtsp://h/2")["port"], first)

    def test_stream_url_matches_the_allocated_port(self):
        info = self.mgr.launch("CAM-1", "rtsp://h/1", stream_host="box")
        self.assertEqual(info["stream_url"], f"http://box:{info['port']}/stream")

    def test_local_port_is_reported_for_proxying(self):
        info = self.mgr.launch("CAM-1", "rtsp://h/1")
        self.assertEqual(self.mgr.local_port("CAM-1"), info["port"])

    def test_local_port_is_none_for_a_camera_we_did_not_launch(self):
        self.assertIsNone(self.mgr.local_port("NOT-OURS"))


class TestArgvConstruction(unittest.TestCase):
    def setUp(self):
        self.mgr = ManagerHarness()
        self.LogCls = _patch(self.mgr)
        import services.api.camera_manager as cm
        cm.open = lambda *a, **kw: self.LogCls()

    def tearDown(self):
        import services.api.camera_manager as cm
        cm.subprocess.Popen, cm.subprocess.run, _ = self.mgr._orig
        if hasattr(cm, "open"):
            del cm.open

    def test_source_is_passed_as_one_argv_element(self):
        # THE injection guarantee. A source is operator-supplied; passed as a
        # list element it is data, and a shell string would make it code.
        nasty = "rtsp://h/1; rm -rf /"
        self.mgr.launch("CAM-1", nasty)
        argv = self.mgr.launched[0]
        self.assertIn(nasty, argv)
        self.assertEqual(argv[argv.index("--source") + 1], nasty)

    def test_argv_is_a_list_never_a_string(self):
        self.mgr.launch("CAM-1", "rtsp://h/1")
        self.assertIsInstance(self.mgr.launched[0], list)

    def test_camera_id_and_api_url_are_passed_through(self):
        mgr = ManagerHarness(api_url="http://api:9000")
        import services.api.camera_manager as cm
        mgr._orig = self.mgr._orig
        cm.subprocess.Popen = lambda cmd, **kw: mgr._spawn(cmd, None)
        mgr.launch("CAM-XYZ", "rtsp://h/1")
        argv = mgr.launched[0]
        self.assertEqual(argv[argv.index("--camera-id") + 1], "CAM-XYZ")
        self.assertEqual(argv[argv.index("--api-url") + 1], "http://api:9000")

    def test_site_id_is_omitted_when_not_given(self):
        self.mgr.launch("CAM-1", "rtsp://h/1")
        self.assertNotIn("--site-id", self.mgr.launched[0])

    def test_site_id_is_included_when_given(self):
        self.mgr.launch("CAM-1", "rtsp://h/1", site_id="SITE-9")
        argv = self.mgr.launched[0]
        self.assertEqual(argv[argv.index("--site-id") + 1], "SITE-9")


class TestLifecycle(unittest.TestCase):
    def setUp(self):
        self.mgr = ManagerHarness()
        self.LogCls = _patch(self.mgr)
        import services.api.camera_manager as cm
        cm.open = lambda *a, **kw: self.LogCls()

    def tearDown(self):
        import services.api.camera_manager as cm
        cm.subprocess.Popen, cm.subprocess.run, _ = self.mgr._orig
        if hasattr(cm, "open"):
            del cm.open

    def test_is_running_tracks_launch_and_stop(self):
        self.assertFalse(self.mgr.is_running("CAM-1"))
        self.mgr.launch("CAM-1", "rtsp://h/1")
        self.assertTrue(self.mgr.is_running("CAM-1"))
        self.mgr.stop("CAM-1")
        self.assertFalse(self.mgr.is_running("CAM-1"))

    def test_relaunch_replaces_rather_than_duplicates(self):
        self.mgr.launch("CAM-1", "rtsp://h/1")
        self.mgr.launch("CAM-1", "rtsp://h/2")
        self.assertEqual(len(self.mgr._procs), 1)

    def test_stop_reaps_orphans_by_camera_id(self):
        # An API restart leaves pipelines running with no parent. The pkill
        # pattern deliberately omits the leading dashes — see camera_manager.py.
        self.mgr.launch("CAM-1", "rtsp://h/1")
        self.mgr.stop("CAM-1")
        self.assertTrue(any("camera-id CAM-1" in " ".join(c)
                            for c in self.mgr.pkilled))

    def test_stop_on_an_unknown_camera_is_false_not_an_error(self):
        self.assertFalse(self.mgr.stop("NEVER-LAUNCHED"))

    def test_stop_all_clears_everything(self):
        for i in range(3):
            self.mgr.launch(f"CAM-{i}", "rtsp://h/x")
        self.mgr.stop_all()
        self.assertEqual(self.mgr._procs, {})


if __name__ == "__main__":
    unittest.main()
