"""Signed, expiring links to one image — a URL a person can click from a chat.

The chatbot cannot render an MCP image block, and a URL with `?key=` in it
would put the API key into a chat transcript, browser history and Referer
headers. A signed link carries neither: it grants exactly one GET of exactly
one path until `exp`, and proves that with an HMAC over both. Whoever has
the link can open that one image until it expires; nothing else. The same
shape as S3 pre-signed URLs, for the same reason.

    ?exp=<unix seconds>&sig=<hex hmac-sha256 over "<path>\\n<exp>">

The secret is FINBLADE_LINK_SECRET, or, when unset, derived from the full
API key (so nothing new to configure: rotating the key voids every link).
With no key at all the API is open and a link needs no signature.

Only paths that serve ONE image are signable — `SIGNABLE` below — so a
signature can never open a listing, a search or a write. Pure stdlib.
"""

import hashlib
import hmac
import os
import time
from typing import Optional, Tuple
from urllib.parse import urlencode

# Route shapes that may be opened by signature: a crop or a frame, by id.
SIGNABLE = ("/api/v1/search/sightings/", "/api/v1/incidents/")
_SIGNABLE_SUFFIXES = ("/crop", "/frame")

DEFAULT_TTL_S = 60 * 60


def secret() -> Optional[bytes]:
    s = os.environ.get("FINBLADE_LINK_SECRET")
    if s:
        return s.encode("utf-8")
    key = os.environ.get("FINBLADE_API_KEY")
    if key:
        # Derived, not the key itself: a valid signature reveals nothing
        # about the key, and the key is never hashed alone anywhere else.
        return hashlib.sha256(b"finblade-link:" + key.encode("utf-8")).digest()
    return None


def ttl_s() -> int:
    try:
        return max(60, int(float(os.environ.get("FINBLADE_LINK_TTL_MINUTES") or 60) * 60))
    except ValueError:
        return DEFAULT_TTL_S


def public_base() -> str:
    """Where a person's browser can reach this API. FINBLADE_PUBLIC_URL, or
    the self URL the workers use (right on a single host, wrong behind NAT)."""
    return (os.environ.get("FINBLADE_PUBLIC_URL") or os.environ.get("FINBLADE_SELF_URL")
            or "http://127.0.0.1:8000").rstrip("/")


def is_signable(path: str) -> bool:
    return path.startswith(SIGNABLE) and path.endswith(_SIGNABLE_SUFFIXES)


def _mac(path: str, exp: int, key: bytes) -> str:
    return hmac.new(key, f"{path}\n{int(exp)}".encode("utf-8"), hashlib.sha256).hexdigest()


def sign(path: str, now: Optional[float] = None, ttl: Optional[int] = None) -> Tuple[str, Optional[int]]:
    """(url, expires_at). expires_at is None when the API is open (no key)."""
    if not is_signable(path):
        raise ValueError(f"not a signable path: {path}")
    key = secret()
    base = public_base()
    if key is None:
        return base + path, None
    exp = int((time.time() if now is None else now) + (ttl or ttl_s()))
    return base + path + "?" + urlencode({"exp": exp, "sig": _mac(path, exp, key)}), exp


def verify(path: str, exp, sig, now: Optional[float] = None) -> bool:
    """True if `sig` is the signature for this path and `exp` is still ahead."""
    key = secret()
    if key is None or not is_signable(path) or not exp or not sig:
        return False
    try:
        exp_i = int(exp)
    except (TypeError, ValueError):
        return False
    if exp_i < (time.time() if now is None else now):
        return False
    return hmac.compare_digest(_mac(path, exp_i, key), str(sig))
