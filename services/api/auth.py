"""API key authentication.

Off unless FINBLADE_API_KEY is set, so an existing deployment keeps working
until someone turns it on deliberately. Once set, every /api/v1 route requires
the key.

THREE WAYS TO PRESENT IT, and the third exists for a real reason:

  Authorization: Bearer <key>     preferred, server-to-server
  X-API-Key: <key>                equivalent, some clients find it simpler
  ?key=<key>                      query string — ONLY on the MJPEG stream and
                                  the /ws WebSocket

The query-string form is not laziness. Both exceptions are browser primitives
that CANNOT send headers: the video feed is an ``<img src="...">``, and the
WebSocket constructor takes a URL and nothing else. There is no header-based way
to authenticate either one. It stays restricted to those two because a key in a
URL leaks into access logs, browser history and Referer headers — acceptable for
a read-only feed, not for anything that mutates.

Comparison is constant-time: a naive == leaks key length and prefix through
timing, which is cheap to avoid.
"""

import hmac
import os
from typing import Optional

# Paths that must work without a key, or nothing can bootstrap:
#   /web, /tools   the dashboard itself (it then asks the user for the key)
#   /bookmarks     alert snapshots, loaded as <img> by the dashboard
#   /media         reference stills for the zone editor
#   /docs, /openapi.json, /redoc   API documentation
_OPEN_PREFIXES = ("/web", "/tools", "/bookmarks", "/media", "/logo",
                  "/docs", "/openapi.json", "/redoc", "/favicon")

# The only routes where ?key= is honoured. See the module docstring.
_QUERY_KEY_SUFFIXES = ("/stream", "/ws")


def configured_key() -> Optional[str]:
    key = os.environ.get("FINBLADE_API_KEY")
    return key if key else None


def enabled() -> bool:
    return configured_key() is not None


def _matches(candidate: Optional[str], key: str) -> bool:
    if not candidate:
        return False
    return hmac.compare_digest(candidate, key)


def path_is_open(path: str) -> bool:
    if path == "/":
        return True
    return any(path.startswith(p) for p in _OPEN_PREFIXES)


def request_is_authorised(path: str, headers, query_params) -> bool:
    """True if this request may proceed."""
    key = configured_key()
    if key is None:
        return True                      # auth disabled
    if path_is_open(path):
        return True

    auth = headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        if _matches(auth[7:].strip(), key):
            return True
    if _matches(headers.get("x-api-key"), key):
        return True

    # Query string: video feed and WebSocket only.
    if path.endswith(_QUERY_KEY_SUFFIXES) and _matches(query_params.get("key"), key):
        return True

    return False
