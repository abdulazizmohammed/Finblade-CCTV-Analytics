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


class TestOpenPaths(unittest.TestCase):
    """The dashboard itself must load without a key, or nothing can bootstrap —
    it is what asks the user for the key in the first place."""

    def setUp(self):
        os.environ["FINBLADE_API_KEY"] = "s3cret"
        self.addCleanup(os.environ.pop, "FINBLADE_API_KEY", None)

    def test_pages_and_assets_are_open(self):
        for path in ("/web/dashboard.html", "/web/apikey.js",
                     "/tools/zone-editor.html", "/bookmarks/x.jpg",
                     "/media/CAM-01_frame.jpg", "/openapi.json", "/"):
            self.assertTrue(auth.request_is_authorised(path, hdr(), {}), path)

    def test_api_is_not_open(self):
        for path in ("/api/v1/alerts", "/api/v1/cameras",
                     "/api/v1/finblade/status"):
            self.assertFalse(auth.request_is_authorised(path, hdr(), {}), path)


if __name__ == "__main__":
    unittest.main()
