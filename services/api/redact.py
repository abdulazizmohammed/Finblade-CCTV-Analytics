"""Strip credentials out of anything that leaves this process.

A camera source is normally `rtsp://user:password@host/Streaming/Channels/101`
— the standard form for an IP camera. That string is stored in the cameras
table and, until this module existed, was returned verbatim by
GET /api/v1/cameras, GET /api/v1/summary and every WebSocket frame, to any
caller holding any key including the read-only integration key.

That is worse than it first looks. The credential does not unlock this API; it
unlocks the CAMERA. Anyone who read it could pull video straight off the device
over RTSP, bypassing this service, its rules, its audit trail and its privacy
guarantees entirely — and the same string leaks through any proxy access log
that records a response body, or through a browser URL carrying ?key=.

MASK, DO NOT DROP, in the operator projection. The cameras page tests
`camera.source` for truthiness to decide whether to offer "Start pipeline"
(web/cameras.html). Removing the field would silently disable that button and
look like a UI bug. Masking keeps the shape, keeps the host readable for
debugging, and destroys the secret.

Integration callers get the field removed outright — they have no use for it,
and the safest handling of a secret is not to serialise it at all.
"""

import re
from typing import Optional

# scheme://user[:password]@host — the password group is optional because
# rtsp://admin@host is also valid and still identifies an account.
_CREDENTIALS = re.compile(
    r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*://)"
    r"(?P<user>[^/@:\s]+)"
    r"(?::(?P<password>[^/@\s]*))?"
    r"@")

MASK = "***"

# Fields that can carry a credential-bearing URL.
URL_FIELDS = ("source", "stream_url", "rtsp_url")

# Fields an integration caller never needs and must not receive.
INTEGRATION_DROP_FIELDS = ("source",)


def mask_credentials(value):
    """rtsp://admin:hunter2@host/x -> rtsp://***:***@host/x

    Non-strings and strings without credentials are returned unchanged, so this
    is safe to map over arbitrary values.
    """
    if not isinstance(value, str) or "@" not in value:
        return value

    def _sub(match):
        if match.group("password") is None:
            return f"{match.group('scheme')}{MASK}@"
        return f"{match.group('scheme')}{MASK}:{MASK}@"

    return _CREDENTIALS.sub(_sub, value)


def redact_camera(row: dict, drop_source: bool = False) -> dict:
    """A camera row safe to serialise. Never mutates the caller's dict."""
    out = dict(row)
    if drop_source:
        for field in INTEGRATION_DROP_FIELDS:
            out.pop(field, None)
    for field in URL_FIELDS:
        if field in out:
            out[field] = mask_credentials(out[field])
    return out


def contains_credentials(text: Optional[str]) -> bool:
    """True if a REAL credential survived anywhere in `text`.

    The mask itself still matches the pattern — `rtsp://***:***@host` is
    structurally `user:password@host` — so a naive search flags correctly
    redacted output and the guard becomes useless. Matches whose parts are
    exactly the mask are therefore not credentials.
    """
    if not text:
        return False
    for match in _CREDENTIALS.finditer(text):
        user = match.group("user")
        password = match.group("password")
        if user == MASK and password in (None, MASK):
            continue                     # already redacted
        return True
    return False
