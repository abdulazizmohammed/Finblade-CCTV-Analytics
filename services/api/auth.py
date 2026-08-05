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

TWO KEYS, TWO ROLES.

  FINBLADE_API_KEY          full     everything, including the destructive routes
  FINBLADE_INTEGRATION_KEY  scoped   every GET + /ws, and exactly two writes:
                                     POST /api/v1/alerts/{id}/ack and /resolve

The scoped role exists because of how a third party actually consumes this API.
A platform rendering its own dashboard from our data needs to read everything
and to let an operator acknowledge an alert — and nothing else. Handing it the
full key also hands it `DELETE /api/v1/alerts` and `DELETE /api/v1/cameras/{id}`,
so one bug in someone else's integration can wipe this deployment's history.

The split is by ROLE, not by route list, so it cannot drift: reads are allowed
wholesale because every GET here is a read, and writes are denied wholesale
except for the two that are the whole point of the integration.

A wrong key is 401. A valid scoped key on a route it may not use is 403 — the
two are different problems and telling them apart saves an integrator an hour.
"""

import hmac
import os
from typing import Optional, Tuple

# Paths that must work without a key, or nothing can bootstrap:
#   /web, /tools   the dashboard itself (it then asks the user for the key)
#   /docs, /openapi.json, /redoc   API documentation
#
# /bookmarks and /media are NOT here, though they were. They serve incident
# snapshots and reference stills — images of people in a monitored space, the
# only identifying artifact this system produces. Anyone who could reach the
# port could enumerate them (`bm_<camera>_<seq>.jpg` is a guessable name) with
# no credential at all, while every JSON route was gated. They now require the
# key like anything else, via ?key= because they load as <img src>.
_OPEN_PREFIXES = ("/web", "/tools", "/logo",
                  "/docs", "/openapi.json", "/redoc", "/favicon")

# The only routes where ?key= is honoured. See the module docstring. All three
# are consumed by a browser primitive that cannot send a header: <img src> for
# the MJPEG stream and for single-frame snapshots, and the WebSocket
# constructor. All three are read-only.
_QUERY_KEY_SUFFIXES = ("/stream", "/snapshot", "/ws")

# Same reasoning, by prefix: saved frames are loaded as <img src> by the history
# page and the zone editor, which cannot attach a header either. Read-only.
_QUERY_KEY_PREFIXES = ("/bookmarks/", "/media/")

ROLE_FULL = "full"
ROLE_INTEGRATION = "integration"

# Methods that only ever read. A WebSocket handshake arrives as a GET.
_READ_METHODS = ("GET", "HEAD", "OPTIONS")

# The only writes a scoped key may perform, both under /api/v1/alerts/{id}/.
# Operator acknowledgement is the one action a consuming platform legitimately
# needs; everything else that mutates belongs to the full key.
_INTEGRATION_WRITE_PREFIX = "/api/v1/alerts/"
_INTEGRATION_WRITE_SUFFIXES = ("/ack", "/resolve")


def configured_key() -> Optional[str]:
    key = os.environ.get("FINBLADE_API_KEY")
    return key if key else None


def configured_integration_key() -> Optional[str]:
    key = os.environ.get("FINBLADE_INTEGRATION_KEY")
    return key if key else None


def enabled() -> bool:
    """True if ANY key is configured.

    Setting only the integration key still turns auth on. Someone who issues a
    scoped key has decided this API needs credentials; leaving it open because
    the full key happened to be unset would be the opposite of what they asked
    for, and it fails silently.
    """
    return configured_key() is not None or configured_integration_key() is not None


def _matches(candidate: Optional[str], key: str) -> bool:
    if not candidate:
        return False
    return hmac.compare_digest(candidate, key)


def path_is_open(path: str) -> bool:
    if path == "/":
        return True
    return any(path.startswith(p) for p in _OPEN_PREFIXES)


def presented_role(path: str, headers, query_params) -> Optional[str]:
    """Which configured key did this request present? None if neither.

    Order matters only in that the full key wins if both are set to the same
    value — a misconfiguration, but the safer resolution of it.
    """
    full = configured_key()
    integration = configured_integration_key()

    candidates = []
    auth = headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        candidates.append(auth[7:].strip())
    x_api_key = headers.get("x-api-key")
    if x_api_key:
        candidates.append(x_api_key)
    # Query string: video feed, snapshot, WebSocket and saved frames only.
    if path.endswith(_QUERY_KEY_SUFFIXES) or path.startswith(_QUERY_KEY_PREFIXES):
        query_key = query_params.get("key")
        if query_key:
            candidates.append(query_key)

    for candidate in candidates:
        if full and _matches(candidate, full):
            return ROLE_FULL
        if integration and _matches(candidate, integration):
            return ROLE_INTEGRATION
    return None


def _integration_may(path: str, method: str) -> bool:
    """Can a scoped key perform this? Reads yes; writes only ack/resolve."""
    if (method or "GET").upper() in _READ_METHODS:
        return True
    return (path.startswith(_INTEGRATION_WRITE_PREFIX)
            and path.endswith(_INTEGRATION_WRITE_SUFFIXES))


def authorise(path: str, headers, query_params,
              method: str = "GET") -> Tuple[bool, Optional[str]]:
    """(allowed, reason). reason is None, 'unauthorized' (401) or 'forbidden' (403)."""
    if not enabled():
        return True, None                # auth disabled
    if path_is_open(path):
        return True, None

    role = presented_role(path, headers, query_params)
    if role is None:
        return False, "unauthorized"
    if role == ROLE_FULL:
        return True, None
    if _integration_may(path, method):
        return True, None
    return False, "forbidden"


def request_is_authorised(path: str, headers, query_params,
                          method: str = "GET") -> bool:
    """True if this request may proceed."""
    return authorise(path, headers, query_params, method)[0]
