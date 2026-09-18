"""Signed, expiring links to one image (finblade/links.py) and the routes that
issue and honour them.

The chatbot cannot render an image block and must not put an API key into a
chat transcript. A link is bound to one path and one expiry; that is the
whole contract, and every test here is one way it could be broken.
"""
import os
import sys
import time
import unittest

os.environ.setdefault("FINBLADE_INMEMORY", "1")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from finblade import links  # noqa: E402

CROP = "/api/v1/search/sightings/ev-1/crop"
FRAME = "/api/v1/incidents/42/frame"


class _Env:
    def __init__(self, **kv):
        self.kv = kv

    def __enter__(self):
        self.old = dict(os.environ)
        for k in ("FINBLADE_API_KEY", "FINBLADE_INTEGRATION_KEY", "FINBLADE_LINK_SECRET",
                  "FINBLADE_PUBLIC_URL", "FINBLADE_SELF_URL", "FINBLADE_LINK_TTL_MINUTES"):
            os.environ.pop(k, None)
        os.environ.update(self.kv)

    def __exit__(self, *a):
        os.environ.clear(); os.environ.update(self.old)


class TestSignVerify(unittest.TestCase):
    def test_link_opens_its_path_until_it_expires_and_nothing_else(self):
        with _Env(FINBLADE_API_KEY="k" * 32, FINBLADE_PUBLIC_URL="http://cctv.example:8000"):
            url, exp = links.sign(CROP, now=1000.0, ttl=600)
            self.assertTrue(url.startswith("http://cctv.example:8000" + CROP + "?exp=1600&sig="))
            self.assertNotIn("k" * 32, url, "the key never appears in a link")
            sig = url.split("sig=")[1]
            self.assertTrue(links.verify(CROP, 1600, sig, now=1599.0))
            self.assertFalse(links.verify(CROP, 1600, sig, now=1601.0), "expired")
            self.assertFalse(links.verify(FRAME, 1600, sig, now=1500.0), "bound to one path")
            self.assertFalse(links.verify(CROP, 1700, sig, now=1500.0), "exp is signed too")
            self.assertFalse(links.verify(CROP, 1600, sig[:-1] + "0", now=1500.0))
            self.assertFalse(links.verify(CROP, "soon", sig, now=1500.0))

    def test_only_single_image_routes_are_signable(self):
        with _Env(FINBLADE_API_KEY="k" * 32):
            for bad in ("/api/v1/search/people", "/api/v1/alerts", "/bookmarks/x.jpg",
                        "/api/v1/search/sightings/ev-1/correct", "/api/v1/cameras/CAM-1/snapshot"):
                self.assertFalse(links.is_signable(bad), bad)
                with self.assertRaises(ValueError):
                    links.sign(bad)
                self.assertFalse(links.verify(bad, 9e12, "x"))
            self.assertTrue(links.is_signable(CROP))
            self.assertTrue(links.is_signable(FRAME))

    def test_secret_sources(self):
        with _Env():
            self.assertIsNone(links.secret())
            url, exp = links.sign(CROP)
            self.assertEqual(("http://127.0.0.1:8000" + CROP, None), (url, exp), "open API: plain link")
            self.assertFalse(links.verify(CROP, 9e12, "anything"), "nothing to verify against")
        with _Env(FINBLADE_API_KEY="a" * 32):
            s_a = links.secret()
        with _Env(FINBLADE_API_KEY="b" * 32):
            self.assertNotEqual(s_a, links.secret(), "rotating the key voids every link")
        with _Env(FINBLADE_API_KEY="a" * 32, FINBLADE_LINK_SECRET="dedicated"):
            self.assertEqual(b"dedicated", links.secret())
        with _Env(FINBLADE_API_KEY="a" * 32, FINBLADE_LINK_TTL_MINUTES="5"):
            self.assertEqual(300, links.ttl_s())
        with _Env(FINBLADE_API_KEY="a" * 32, FINBLADE_LINK_TTL_MINUTES="0.1"):
            self.assertEqual(60, links.ttl_s(), "floor of a minute")


try:
    from fastapi.testclient import TestClient
    from services.api.app import app, svc as app_svc
    HAVE_APP = True
except Exception:                              # noqa: BLE001
    HAVE_APP = False


@unittest.skipUnless(HAVE_APP, "fastapi app not importable")
class TestRoutes(unittest.TestCase):
    def setUp(self):
        self.c = TestClient(app)
        app_svc.store._sightings.clear()

    def _seed(self):
        import tempfile
        from finblade.events import PERSON_ATTRIBUTES, new_event
        from services.api import app as _appmod
        os.makedirs(_appmod._BOOKMARKS_DIR, exist_ok=True)
        fd, p = tempfile.mkstemp(prefix="attr_link_", suffix=".jpg", dir=_appmod._BOOKMARKS_DIR)
        os.write(fd, b"\xff\xd8\xff\xe0JPEG"); os.close(fd)
        self.addCleanup(lambda: os.path.exists(p) and os.unlink(p))
        e = new_event(PERSON_ATTRIBUTES, "CAM-L1", "RUH-01", time.time() - 10, person_ref="pr_" + "f" * 16,
                      attributes={"bag": "backpack"}, confidences={"bag": 0.9}, samples=3,
                      description="backpack", frame="/bookmarks/" + os.path.basename(p))
        e["global_ref"] = "gp_link"
        self.assertEqual(202, self.c.post("/api/v1/events/ingest", json=e,
                                          headers={"Authorization": "Bearer full-key-full-key"}).status_code)
        return e["event_id"]

    def test_search_results_carry_a_clickable_crop_url_that_opens_without_a_key(self):
        with _Env(FINBLADE_API_KEY="full-key-full-key", FINBLADE_PUBLIC_URL="http://testserver"):
            sid = self._seed()
            H = {"Authorization": "Bearer full-key-full-key"}
            r = self.c.get("/api/v1/search/people?bag=backpack&hours=1", headers=H).json()
            s = r["people"][0]["sightings"][0]
            self.assertTrue(s["crop_url"].startswith(f"http://testserver/api/v1/search/sightings/{sid}/crop?exp="))
            self.assertNotIn("full-key", s["crop_url"])
            path_q = s["crop_url"][len("http://testserver"):]
            # no header, no key: the signature alone opens exactly this image
            img = self.c.get(path_q)
            self.assertEqual((200, "image/jpeg"), (img.status_code, img.headers["content-type"]), img.text)
            # ...and nothing else
            exp, sig = path_q.split("exp=")[1].split("&sig=")
            self.assertEqual(401, self.c.get(f"/api/v1/search/sightings/{sid}/correct?exp={exp}&sig={sig}").status_code)
            self.assertEqual(401, self.c.get(f"/api/v1/search/people?exp={exp}&sig={sig}").status_code)
            self.assertEqual(401, self.c.get(f"/api/v1/search/sightings/{sid}/crop?exp={int(exp)+1}&sig={sig}").status_code)
            # the timeline and the link route issue the same thing
            t = self.c.get("/api/v1/search/people/gp_link?hours=1", headers=H).json()
            self.assertIn("crop_url", t["sightings"][0])
            link = self.c.get(f"/api/v1/search/sightings/{sid}/crop/link", headers=H).json()
            self.assertEqual(200, self.c.get(link["url"][len("http://testserver"):]).status_code)
            self.assertGreater(link["expires_at"], time.time())
            self.assertEqual(401, self.c.get(f"/api/v1/search/sightings/{sid}/crop/link",
                                             headers={"Authorization": "Bearer nope"}).status_code)
            self.assertEqual(404, self.c.get("/api/v1/search/sightings/nope/crop/link", headers=H).status_code)

    def test_incident_frame_link(self):
        with _Env(FINBLADE_API_KEY="full-key-full-key"):
            H = {"Authorization": "Bearer full-key-full-key"}
            self.assertEqual(404, self.c.get("/api/v1/incidents/no-such-alert/frame/link", headers=H).status_code)


if __name__ == "__main__":
    unittest.main()
