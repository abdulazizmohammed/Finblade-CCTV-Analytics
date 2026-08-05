import os
import unittest

from services.api import auth


class Headers(dict):
    """Case-insensitive, like Starlette's."""
    def get(self, k, default=None):
        return dict.get(self, k.lower(), default)


def hdr(**kw):
    return Headers({k.lower().replace("_", "-"): v for k, v in kw.items()})


class TestDisabledByDefault(unittest.TestCase):
    def setUp(self):
        os.environ.pop("FINBLADE_API_KEY", None)
        os.environ.pop("FINBLADE_INTEGRATION_KEY", None)

    def test_no_key_configured_allows_everything(self):
        # An existing deployment must keep working until auth is turned on
        # deliberately.
        self.assertFalse(auth.enabled())
        self.assertTrue(auth.request_is_authorised("/api/v1/alerts", hdr(), {}))


class TestEnforcement(unittest.TestCase):
    def setUp(self):
        os.environ["FINBLADE_API_KEY"] = "s3cret"
        self.addCleanup(os.environ.pop, "FINBLADE_API_KEY", None)

    def test_no_credentials_rejected(self):
        self.assertFalse(auth.request_is_authorised("/api/v1/alerts", hdr(), {}))

    def test_bearer_accepted(self):
        self.assertTrue(auth.request_is_authorised(
            "/api/v1/alerts", hdr(authorization="Bearer s3cret"), {}))

    def test_bearer_is_case_insensitive_on_the_scheme(self):
        self.assertTrue(auth.request_is_authorised(
            "/api/v1/alerts", hdr(authorization="bearer s3cret"), {}))

    def test_x_api_key_accepted(self):
        self.assertTrue(auth.request_is_authorised(
            "/api/v1/alerts", hdr(x_api_key="s3cret"), {}))

    def test_wrong_key_rejected(self):
        self.assertFalse(auth.request_is_authorised(
            "/api/v1/alerts", hdr(authorization="Bearer wrong"), {}))
        self.assertFalse(auth.request_is_authorised(
            "/api/v1/alerts", hdr(x_api_key="wrong"), {}))

    def test_empty_key_header_rejected(self):
        self.assertFalse(auth.request_is_authorised(
            "/api/v1/alerts", hdr(authorization="Bearer "), {}))


class TestQueryKeyIsStreamOnly(unittest.TestCase):
    """?key= exists because <img src> cannot carry a header. It must not become
    a general-purpose bypass: a key in a URL leaks into access logs, browser
    history and Referer headers."""

    def setUp(self):
        os.environ["FINBLADE_API_KEY"] = "s3cret"
        self.addCleanup(os.environ.pop, "FINBLADE_API_KEY", None)

    def test_query_key_works_on_the_stream(self):
        self.assertTrue(auth.request_is_authorised(
            "/api/v1/cameras/CAM-A/stream", hdr(), {"key": "s3cret"}))

    def test_query_key_rejected_everywhere_else(self):
        for path in ("/api/v1/alerts", "/api/v1/cameras",
                     "/api/v1/identity/counts", "/api/v1/identity/tuning"):
            self.assertFalse(
                auth.request_is_authorised(path, hdr(), {"key": "s3cret"}),
                f"?key= must not authenticate {path}")

    def test_wrong_query_key_rejected_on_stream(self):
        self.assertFalse(auth.request_is_authorised(
            "/api/v1/cameras/CAM-A/stream", hdr(), {"key": "wrong"}))

    def test_query_key_works_on_a_snapshot(self):
        """Snapshots are loaded by <img src> exactly as the MJPEG stream is, and
        the dashboard now uses them by default: a live stream never closes, so
        six camera tiles hold every connection the browser allows to one origin
        and the rest of the page queues behind them."""
        self.assertTrue(auth.request_is_authorised(
            "/api/v1/cameras/CAM-A/snapshot", hdr(), {"key": "s3cret"}))

    def test_wrong_query_key_rejected_on_a_snapshot(self):
        self.assertFalse(auth.request_is_authorised(
            "/api/v1/cameras/CAM-A/snapshot", hdr(), {"key": "wrong"}))

    def test_query_key_works_on_the_websocket(self):
        """A browser cannot set headers on a WebSocket either, so /ws is the
        second and only other place ?key= is honoured."""
        self.assertTrue(auth.request_is_authorised(
            "/ws", hdr(), {"key": "s3cret"}))

    def test_wrong_query_key_rejected_on_the_websocket(self):
        self.assertFalse(auth.request_is_authorised(
            "/ws", hdr(), {"key": "wrong"}))

    def test_websocket_requires_a_key_at_all(self):
        """Regression guard. /ws is NOT covered by the @app.middleware("http")
        gate — Starlette's BaseHTTPMiddleware never sees websocket scope — so it
        served zone states and alerts unauthenticated while every REST route was
        gated. The route now checks this function itself; if this assertion ever
        flips to True, that hole is back open."""
        self.assertFalse(auth.request_is_authorised("/ws", hdr(), {}))


class TestOpenPaths(unittest.TestCase):
    """The dashboard itself must load without a key, or nothing can bootstrap —
    it is what asks the user for the key in the first place."""

    def setUp(self):
        os.environ["FINBLADE_API_KEY"] = "s3cret"
        self.addCleanup(os.environ.pop, "FINBLADE_API_KEY", None)

    def test_pages_and_assets_are_open(self):
        for path in ("/web/dashboard.html", "/web/apikey.js",
                     "/tools/zone-editor.html", "/openapi.json", "/"):
            self.assertTrue(auth.request_is_authorised(path, hdr(), {}), path)

    def test_saved_frames_are_NOT_open(self):
        """This assertion used to be the opposite way round.

        /bookmarks and /media were listed as open so an <img src> could load
        them without a header — which also served every incident snapshot, and
        every reference still of the monitored space, to anyone who could reach
        the port. They are images of people; they are the only identifying
        artifact this system produces. They now take the key via ?key=, exactly
        like the MJPEG stream, and the two pages that display them were updated
        to append it. Full coverage in test_snapshot_access.py.
        """
        for path in ("/bookmarks/x.jpg", "/media/CAM-01_frame.jpg"):
            self.assertFalse(auth.request_is_authorised(path, hdr(), {}), path)
            self.assertTrue(
                auth.request_is_authorised(path, hdr(), {"key": "s3cret"}), path)

    def test_api_is_not_open(self):
        for path in ("/api/v1/alerts", "/api/v1/cameras",
                     "/api/v1/finblade/status"):
            self.assertFalse(auth.request_is_authorised(path, hdr(), {}), path)


class TestIntegrationKeyScope(unittest.TestCase):
    """The scoped key a consuming platform gets (FINBLADE_INTEGRATION_KEY).

    It exists so an integration bug on someone else's side cannot wipe this
    deployment. Every assertion below is a route that must stay out of reach of
    a key handed to a third party.
    """

    def setUp(self):
        os.environ["FINBLADE_API_KEY"] = "full-key"
        os.environ["FINBLADE_INTEGRATION_KEY"] = "scoped-key"
        self.addCleanup(os.environ.pop, "FINBLADE_API_KEY", None)
        self.addCleanup(os.environ.pop, "FINBLADE_INTEGRATION_KEY", None)

    def scoped(self, path, method="GET", query=None):
        return auth.authorise(path, hdr(authorization="Bearer scoped-key"),
                              query or {}, method)

    def full(self, path, method="GET"):
        return auth.authorise(path, hdr(authorization="Bearer full-key"),
                              {}, method)

    # -- reads: everything is permitted --------------------------------------
    def test_scoped_key_may_read_every_route(self):
        for path in ("/api/v1/cameras", "/api/v1/zones", "/api/v1/zones/state",
                     "/api/v1/alerts", "/api/v1/summary",
                     "/api/v1/identity/counts", "/api/v1/history/events",
                     "/api/v1/reports/occupancy.json", "/api/v1/finblade/status"):
            self.assertEqual((True, None), self.scoped(path), path)

    def test_scoped_key_may_open_the_websocket(self):
        """The tile board needs push. A WebSocket handshake is a GET."""
        self.assertEqual((True, None), self.scoped("/ws"))
        self.assertEqual((True, None),
                         self.scoped("/ws", query={"key": "scoped-key"}))

    def test_scoped_key_works_on_snapshots_via_query_string(self):
        """Camera tiles are <img src>, which cannot carry a header."""
        self.assertEqual(
            (True, None),
            self.scoped("/api/v1/cameras/CAM-01/snapshot",
                        query={"key": "scoped-key"}))

    # -- the two permitted writes --------------------------------------------
    def test_scoped_key_may_acknowledge_and_resolve(self):
        for path in ("/api/v1/alerts/1042/ack", "/api/v1/alerts/1042/resolve"):
            self.assertEqual((True, None), self.scoped(path, "POST"), path)

    # -- everything else that mutates is denied ------------------------------
    def test_scoped_key_cannot_reach_destructive_routes(self):
        denied = [
            ("/api/v1/alerts", "DELETE"),                     # wipes alert history
            ("/api/v1/frames/orphaned", "DELETE"),            # wipes snapshots
            ("/api/v1/cameras/CAM-01", "DELETE"),             # stops the pipeline
            ("/api/v1/zones", "POST"),                        # overwrites polygons
            ("/api/v1/cameras", "POST"),                      # provisioning
            ("/api/v1/cameras/CAM-01/start", "POST"),
            ("/api/v1/cameras/CAM-01/stop", "POST"),
            ("/api/v1/cameras/CAM-01/simulate-failure", "POST"),
            ("/api/v1/identity/tuning", "POST"),              # site-wide matching
            ("/api/v1/identity/merge", "POST"),
            ("/api/v1/events/ingest", "POST"),                # fabricates measurements
            ("/api/v1/zones/state", "POST"),
            ("/api/v1/cameras/health", "POST"),
            ("/api/v1/alerts", "POST"),                       # raises a fake alert
        ]
        for path, method in denied:
            self.assertEqual((False, "forbidden"), self.scoped(path, method),
                             f"{method} {path} must be out of scope")

    def test_ack_suffix_alone_does_not_grant_a_write(self):
        """The allowance is anchored to /api/v1/alerts/, not to the suffix. A
        route that merely ends in /ack must not inherit it."""
        self.assertEqual((False, "forbidden"),
                         self.scoped("/api/v1/cameras/CAM-01/ack", "POST"))

    # -- the full key is unaffected ------------------------------------------
    def test_full_key_still_does_everything(self):
        for path, method in (("/api/v1/alerts", "DELETE"),
                             ("/api/v1/zones", "POST"),
                             ("/api/v1/cameras/CAM-01", "DELETE"),
                             ("/api/v1/identity/tuning", "POST")):
            self.assertEqual((True, None), self.full(path, method),
                             f"{method} {path}")

    # -- 401 and 403 are different problems ----------------------------------
    def test_unknown_key_is_401_not_403(self):
        """A wrong credential and an out-of-scope one fail differently, or an
        integrator debugs the wrong thing."""
        self.assertEqual(
            (False, "unauthorized"),
            auth.authorise("/api/v1/alerts", hdr(authorization="Bearer nope"),
                           {}, "DELETE"))

    def test_scoped_key_alone_still_turns_auth_on(self):
        os.environ.pop("FINBLADE_API_KEY", None)
        self.assertTrue(auth.enabled())
        self.assertEqual((False, "unauthorized"),
                         auth.authorise("/api/v1/cameras", hdr(), {}, "GET"))
        self.assertEqual((True, None), self.scoped("/api/v1/cameras"))


if __name__ == "__main__":
    unittest.main()
