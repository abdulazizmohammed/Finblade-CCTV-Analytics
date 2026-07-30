"""Static mounts must exist on a FRESH clone, not only on a worn-in one.

The zone editor loads a reference still from /media/<camera>_frame.jpg to draw
polygons on. On a fresh deployment that route 404'd and the editor hung, with no
error logged anywhere, because the mount was written as:

    if os.path.isdir(_MEDIA_DIR):
        app.mount("/media", ...)

Nothing under media/ is tracked by git, so the directory does not exist until a
camera worker writes its first still — and the mount is decided ONCE at import,
before any worker has run. The condition was therefore false on exactly the
deployments that needed it, and true on every development machine, where the
directory had accumulated frames in earlier sessions. That is the worst possible
combination: it cannot reproduce where it is being debugged.

No zones means no occupancy, no density and no rules, so this is load-bearing
rather than cosmetic.
"""

import unittest

from services.api.app import app


def _mount_names():
    return {getattr(r, "name", None) for r in app.routes}


class TestStaticMounts(unittest.TestCase):

    def test_media_is_mounted(self):
        """The zone editor's reference stills. Must be mounted regardless of
        whether the directory happened to exist at import time."""
        self.assertIn("media", _mount_names(),
                      "/media is not mounted — the zone editor will have no "
                      "frame to draw on and will hang with no error")

    def test_web_is_mounted(self):
        """The dashboard itself."""
        self.assertIn("web", _mount_names())

    def test_bookmarks_is_mounted(self):
        """Alert snapshots, loaded as <img> by the dashboard."""
        self.assertIn("bookmarks", _mount_names())

    def test_media_directory_exists_after_import(self):
        """Importing the app must CREATE the directory, not merely tolerate its
        absence — a mount over a missing directory would fail on first request
        instead of at startup, which is harder to diagnose."""
        import os
        from services.api.app import _MEDIA_DIR
        self.assertTrue(os.path.isdir(_MEDIA_DIR),
                        f"{_MEDIA_DIR} was not created at import")


if __name__ == "__main__":
    unittest.main()
