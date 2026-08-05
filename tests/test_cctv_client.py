"""Tests for the reference client shipped to the FinBlade team.

It lives in integrations/ rather than services/, but it is ours and it is the
thing another team will copy — an untested reference implementation teaches its
bugs to whoever copies it. Transport is injected, so nothing here touches the
network.
"""

import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integrations" / "finblade_ai"))

from cctv_client import CCTVClient, CCTVError            # noqa: E402


class FakeResponse:
    def __init__(self, status_code=200, payload=None, content=b""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.content = content

    def json(self):
        return self._payload


class Recorder:
    """Counts calls so caching and coalescing are observable."""

    def __init__(self, response=None, delay=0.0):
        self.calls = []
        self.response = response or FakeResponse(payload={"zones": []})
        self.delay = delay
        self._lock = threading.Lock()

    def get(self, path, params, stream=False):
        with self._lock:
            self.calls.append((path, params))
        if self.delay:
            import time
            time.sleep(self.delay)
        return self.response

    def post(self, path, payload):
        with self._lock:
            self.calls.append((path, payload))
        return self.response


def make(rec, **kw):
    return CCTVClient(base_url="http://cctv:8000", api_key="k",
                      get=rec.get, post=rec.post, **kw)


class TestAllowlist(unittest.TestCase):
    def test_unknown_route_is_refused_without_calling_upstream(self):
        rec = Recorder()
        with self.assertRaises(CCTVError):
            make(rec).read("delete_alerts")
        self.assertEqual([], rec.calls, "must not reach upstream at all")

    def test_destructive_routes_are_not_reachable(self):
        """The allowlist is the boundary. These names must not resolve."""
        rec = Recorder()
        c = make(rec)
        for name in ("frames_orphaned", "zones_save", "cameras_delete",
                     "identity_tuning", "events_ingest"):
            with self.assertRaises(CCTVError, msg=name):
                c.read(name)
        self.assertEqual([], rec.calls)


class TestCaching(unittest.TestCase):
    def test_repeat_reads_inside_the_ttl_hit_the_cache(self):
        rec = Recorder()
        c = make(rec)
        for _ in range(5):
            c.read("zone_state")
        self.assertEqual(1, len(rec.calls),
                         "zone state is a 5s aggregate upstream; five polls is one call")

    def test_different_params_are_cached_separately(self):
        rec = Recorder()
        c = make(rec)
        c.read("movement", camera_id="CAM-01")
        c.read("movement", camera_id="CAM-02")
        self.assertEqual(2, len(rec.calls))

    def test_concurrent_misses_collapse_into_one_upstream_call(self):
        """Twenty dashboards opening at once must not be twenty requests."""
        rec = Recorder(delay=0.05)
        c = make(rec)
        threads = [threading.Thread(target=c.read, args=("summary",))
                   for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(1, len(rec.calls))

    def test_frames_are_shared_across_viewers(self):
        rec = Recorder(response=FakeResponse(content=b"\xff\xd8jpeg"))
        c = make(rec)
        for _ in range(10):
            self.assertEqual(b"\xff\xd8jpeg", c.frame("CAM-01"))
        self.assertEqual(1, len(rec.calls),
                         "ten viewers must cost one snapshot, not ten")


class TestErrorMapping(unittest.TestCase):
    def test_401_names_the_key_not_the_network(self):
        c = make(Recorder(response=FakeResponse(status_code=401)))
        with self.assertRaises(CCTVError) as ctx:
            c.read("cameras")
        self.assertIn("401", str(ctx.exception))
        self.assertIn("CCTV_API_KEY", str(ctx.exception))

    def test_403_says_out_of_scope(self):
        c = make(Recorder(response=FakeResponse(status_code=403)))
        with self.assertRaises(CCTVError) as ctx:
            c.read("cameras")
        self.assertIn("scope", str(ctx.exception))

    def test_transport_failure_becomes_cctverror(self):
        class Boom:
            calls = []

            def get(self, path, params, stream=False):
                raise OSError("connection refused")

        c = CCTVClient(base_url="http://cctv:8000", get=Boom().get, post=None)
        with self.assertRaises(CCTVError):
            c.read("cameras")

    def test_a_failed_read_is_not_cached(self):
        """A cached failure would outlive the outage that caused it."""
        rec = Recorder(response=FakeResponse(status_code=500))
        c = make(rec)
        for _ in range(3):
            with self.assertRaises(CCTVError):
                c.read("cameras")
        self.assertEqual(3, len(rec.calls))


class TestAlertActions(unittest.TestCase):
    def test_409_is_treated_as_terminal_success(self):
        """Documented as terminal: already in that state, or unknown id. A
        client that retries every non-2xx retries this forever."""
        c = make(Recorder(response=FakeResponse(status_code=409)))
        self.assertEqual({"ok": True, "already_applied": True},
                         c.acknowledge("1042", "operator@finblade"))

    def test_acknowledge_carries_the_user(self):
        rec = Recorder(response=FakeResponse(payload={"acknowledged": True}))
        make(rec).acknowledge("1042", "operator@finblade")
        path, payload = rec.calls[0]
        self.assertEqual("/api/v1/alerts/1042/ack", path)
        self.assertEqual("operator@finblade", payload["acknowledged_by"])

    def test_resolve_sends_action_and_note(self):
        rec = Recorder(response=FakeResponse(payload={"ok": True}))
        make(rec).resolve("7", "op@finblade", action="DISMISSED", note="false alarm")
        path, payload = rec.calls[0]
        self.assertEqual("/api/v1/alerts/7/resolve", path)
        self.assertEqual("DISMISSED", payload["action"])
        self.assertEqual("false alarm", payload["note"])


class TestSiteOccupancy(unittest.TestCase):
    def test_no_zones_is_none_not_zero(self):
        """No polygons means occupancy CANNOT be computed. Rendering it as 0
        shows an empty site while people are in it."""
        c = make(Recorder())
        self.assertIsNone(c.site_occupancy({"zones": []}))

    def test_sums_zone_occupancy(self):
        c = make(Recorder())
        self.assertEqual(7, c.site_occupancy(
            {"zones": [{"occupancy": 4}, {"occupancy": 3}]}))

    def test_zone_key_is_camera_scoped(self):
        """ZONE-01 on two cameras is two different areas."""
        a = {"camera_id": "CAM-03", "zone_id": "ZONE-01"}
        b = {"camera_id": "CAM-04", "zone_id": "ZONE-01"}
        self.assertNotEqual(CCTVClient.zone_key(a), CCTVClient.zone_key(b))


class TestCredentialHandling(unittest.TestCase):
    def test_key_goes_in_a_header_never_the_query_string(self):
        c = make(Recorder())
        self.assertEqual({"Authorization": "Bearer k"}, c._headers)

    def test_no_key_configured_sends_no_header(self):
        c = CCTVClient(base_url="http://cctv:8000", api_key="")
        self.assertEqual({}, c._headers)


if __name__ == "__main__":
    unittest.main()
