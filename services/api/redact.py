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

# scheme://userinfo@host, where userinfo is EVERYTHING up to the LAST '@'
# before the path.
#
# The obvious pattern — user, optional ':' password, then '@' — stops at the
# FIRST '@', and passwords contain '@' constantly. A camera source of
#     rtsp://operator:p@ssw0rd@10.0.0.5:554/Streaming/Channels/102
# came back from the live API as
#     rtsp://***:***@ssw0rd@10.0.0.5:554/Streaming/Channels/102
# with 'ssw0rd' — the tail of the password — still in the response. Masked
# output that still contains part of the secret is worse than none, because it
# looks handled.
#
# The example above is INVENTED. It used to be the real URL this was found on,
# which put a live camera's address, username and password tail into a public
# repository — the exact leak this module exists to prevent, committed by the
# fix for it. Keep fixtures synthetic even when a real value is what tripped
# the bug.
#
# `[^/\s]*` is greedy and cannot cross a '/', so it consumes the whole
# authority and backtracks to the last '@' before the path. A URL with '@' in
# the path (http://host/a@b) has no '@' before the first '/', so it does not
# match at all.
_CREDENTIALS = re.compile(
    r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*://)"
    r"(?P<userinfo>[^/\s]*)"
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
        userinfo = match.group("userinfo")
        if not userinfo:
            return match.group(0)          # "rtsp://@host" — nothing to hide
        # Keep the shape (user vs user:password) without keeping any of it.
        shape = f"{MASK}:{MASK}" if ":" in userinfo else MASK
        return f"{match.group('scheme')}{shape}@"

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
        userinfo = match.group("userinfo")
        if userinfo in ("", MASK, f"{MASK}:{MASK}"):
            continue                     # empty or already redacted
        return True
    return False
