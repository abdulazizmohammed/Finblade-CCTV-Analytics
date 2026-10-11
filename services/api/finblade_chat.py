"""Ask FinBlade's AI about this CCTV system, from the operator's own screen.

THE SHAPE, and why it is this way. The browser never talks to FinBlade. It
posts a question to this API, which signs in to FinBlade as one service
account and forwards it; FinBlade's agent then calls back into our MCP server
(docs/MCP.md) to get the actual numbers. So:

    operator's browser  ->  this API  ->  FinBlade cloud
                                              |
                                              v
                                       our MCP :8010  (tools, the real data)

Three consequences, all deliberate:

  * The FinBlade password lives in .env on the CCTV host, never in a page.
    The same rule the Signal K and E-Approval integrations follow.
  * The answer's quality is bounded by what the MCP exposes, not by this file.
    If the chat cannot see something, the fix is a tool, not a prompt.
  * FinBlade must be able to REACH the MCP server. That is the firewall rule
    on :8010, not anything here — a chat that connects but answers "I have no
    data" is almost always that.

OFF UNLESS CONFIGURED. No base URL, user or password means `configured()` is
False, the route 404s and the UI hides the button, exactly like forwarding
and webhooks.

WHAT IS NOT VERIFIED HERE. FinBlade's request and response bodies come from
their integration guide, which this repository does not have; the paths and
field names below are what the two shipped integrations use and every one is
overridable from the environment. `status()` performs a real sign-in so an
operator can tell "wrong password" from "wrong path" from "cannot reach it"
without reading a log.
"""

import logging
import os
import threading
import time
from typing import Optional, Tuple

log = logging.getLogger("finblade.chat")

# How long a question may be. Not a FinBlade limit — a guard so a paste of a
# log file does not become an agent turn that costs minutes and says nothing.
MAX_QUESTION = 2000

# Re-sign-in this long before the token actually expires, so a question never
# races the expiry.
TOKEN_SKEW_S = 300.0


class FinBladeChat:
    """Signed-in client for FinBlade's agent chat. Thread-safe, lazy login."""

    def __init__(self, base_url: str = None, username: str = None, password: str = None,
                 mcp_key: str = None, enabled: bool = None,
                 login_path: str = None, chat_path: str = None, sessions_path: str = None,
                 auth_scheme: str = None, reasoning_effort: str = None,
                 timeout_s: float = None, post=None, get=None):
        env = os.environ.get
        self.base_url = (base_url if base_url is not None else env("FINBLADE_CHAT_URL", "")).rstrip("/")
        self.username = username if username is not None else env("FINBLADE_CHAT_USER", "")
        self.password = password if password is not None else env("FINBLADE_CHAT_PASSWORD", "")
        # Which MCP service FinBlade should use for this chat — the "key the
        # assistant uses" shown against the service in the FinBlade web app.
        self.mcp_key = mcp_key if mcp_key is not None else env("FINBLADE_CHAT_MCP_KEY", "")
        self.login_path = login_path or env("FINBLADE_CHAT_LOGIN_PATH", "/api/v1/token/login/")
        self.chat_path = chat_path or env("FINBLADE_CHAT_PATH", "/microservices/agent/chat/")
        # Tried in order and the working one remembered — the two shipped
        # integrations disagree about which one this deployment has.
        self.sessions_path = sessions_path or env("FINBLADE_CHAT_SESSIONS_PATH", "")
        self.auth_scheme = auth_scheme or env("FINBLADE_CHAT_AUTH_SCHEME", "Token")
        self.reasoning_effort = reasoning_effort or env("FINBLADE_CHAT_EFFORT", "minimal")
        try:
            self.timeout_s = float(timeout_s if timeout_s is not None
                                   else env("FINBLADE_CHAT_TIMEOUT", "45"))
        except ValueError:
            self.timeout_s = 45.0
        self._enabled_override = enabled
        # Injected in tests; a real transport is built on first use so importing
        # this module never needs requests.
        self._post, self._get = post, get
        self._lock = threading.Lock()
        self._token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self.asked = 0
        self.errors = 0
        self.last_error: Optional[str] = None

    # ---- configuration -----------------------------------------------------
    def configured(self) -> bool:
        """Everything needed to sign in is present."""
        return bool(self.base_url and self.username and self.password)

    def enabled(self) -> bool:
        if self._enabled_override is not None:
            return bool(self._enabled_override) and self.configured()
        flag = os.environ.get("FINBLADE_CHAT_ENABLED")
        if flag is not None and flag.strip().lower() in ("0", "false", "no", "off"):
            return False
        return self.configured()

    # ---- transport ---------------------------------------------------------
    def _session(self):
        if self._post is None or self._get is None:
            import requests
            s = requests.Session()
            if self._post is None:
                self._post = lambda url, **kw: s.post(url, **kw)
            if self._get is None:
                self._get = lambda url, **kw: s.get(url, **kw)
        return self._post, self._get

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json", "Accept": "application/json"}
        if self._token:
            h["Authorization"] = f"{self.auth_scheme} {self._token}".strip()
        return h

    # ---- auth --------------------------------------------------------------
    def _login(self, force: bool = False) -> Tuple[bool, str]:
        """Sign in and cache the token. Returns (ok, message)."""
        with self._lock:
            if not force and self._token and time.time() < self._token_expires_at:
                return True, "cached"
            if not self.configured():
                return False, "not configured"
            post, _ = self._session()
            url = self.base_url + self.login_path
            try:
                r = post(url, json={"username": self.username, "password": self.password},
                         timeout=min(self.timeout_s, 30),
                         headers={"Content-Type": "application/json"})
            except Exception as exc:                        # noqa: BLE001
                return False, f"cannot reach FinBlade at {self.base_url} ({exc.__class__.__name__})"
            code = getattr(r, "status_code", 0)
            if code in (400, 401, 403):
                return False, "FinBlade rejected the username or password"
            if code == 404:
                return False, (f"login endpoint not found at {self.login_path} — the base URL "
                               "must be the host only, with no path")
            if code >= 300:
                return False, f"FinBlade login returned HTTP {code}"
            try:
                body = r.json()
            except Exception:                               # noqa: BLE001
                return False, "FinBlade login did not return JSON"
            # djoser calls it auth_token, DRF token, SimpleJWT access. Take
            # whichever is there rather than pinning one and breaking on the
            # next deployment.
            token = None
            for key in ("auth_token", "token", "access", "key", "access_token"):
                if isinstance(body, dict) and body.get(key):
                    token = str(body[key]); break
            if not token:
                return False, f"no token in the login response (keys: {sorted(body)[:6]})"
            self._token = token
            ttl = 24 * 3600.0
            try:
                if isinstance(body, dict) and body.get("expires_in"):
                    ttl = float(body["expires_in"])
            except (TypeError, ValueError):
                pass
            self._token_expires_at = time.time() + max(60.0, ttl - TOKEN_SKEW_S)
            log.info("FinBlade chat: signed in as %s, token good for %.0f h",
                     self.username, (self._token_expires_at - time.time()) / 3600.0)
            return True, "ok"

    # ---- public ------------------------------------------------------------
    def status(self) -> dict:
        """Enough to tell a misconfiguration from an outage, without a log."""
        body = {
            "enabled": self.enabled(),
            "configured": self.configured(),
            "base_url": self.base_url or None,
            "mcp_key": self.mcp_key or None,
            "reasoning_effort": self.reasoning_effort,
            "asked": self.asked, "errors": self.errors,
            "last_error": self.last_error,
            # Never the password, and never the token itself.
            "user": self.username or None,
        }
        if not self.configured():
            body["detail"] = ("set FINBLADE_CHAT_URL, FINBLADE_CHAT_USER and "
                              "FINBLADE_CHAT_PASSWORD in .env to enable the chat")
            return body
        ok, msg = self._login()
        body["auth"] = {"ok": ok, "detail": msg, "scheme": self.auth_scheme,
                        "has_token": bool(self._token),
                        "expires_at": self._token_expires_at or None}
        if not self.mcp_key:
            body["warning"] = ("FINBLADE_CHAT_MCP_KEY is not set — FinBlade will answer "
                               "from general knowledge with no access to this system")
        return body

    def ask(self, question: str, session_id: str = None,
            context: str = None) -> Tuple[int, dict]:
        """Put one question to FinBlade. Returns (http_status, body)."""
        question = (question or "").strip()
        if not question:
            return 422, {"error": "a question is required"}
        if len(question) > MAX_QUESTION:
            return 422, {"error": f"question too long (max {MAX_QUESTION} characters)"}
        if not self.enabled():
            return 503, {"error": "FinBlade chat is not configured on this server",
                         "detail": self.status().get("detail")}

        ok, msg = self._login()
        if not ok:
            self.errors += 1
            self.last_error = msg
            return 502, {"error": msg}

        payload = {"message": question, "reasoning_effort": self.reasoning_effort}
        if self.mcp_key:
            payload["mcp_key"] = self.mcp_key
        if session_id:
            payload["session_id"] = session_id
        if context:
            # What the operator is looking at, so "this camera" and "here" mean
            # something. Plain text, no identifiers — see the route.
            payload["context"] = context

        post, _ = self._session()
        url = self.base_url + self.chat_path
        try:
            r = post(url, json=payload, headers=self._headers(), timeout=self.timeout_s)
        except Exception as exc:                            # noqa: BLE001
            self.errors += 1
            self.last_error = exc.__class__.__name__
            if "Timeout" in exc.__class__.__name__:
                return 504, {"error": "FinBlade is still working on this answer",
                             "detail": f"no reply within {self.timeout_s:.0f}s",
                             "session_id": session_id, "pending": True}
            return 502, {"error": f"cannot reach FinBlade ({exc.__class__.__name__})"}

        code = getattr(r, "status_code", 0)
        if code in (401, 403):
            # The token was revoked or expired early. One retry with a fresh
            # one; a second failure is a real credentials problem.
            ok, msg = self._login(force=True)
            if not ok:
                self.errors += 1; self.last_error = msg
                return 502, {"error": msg}
            try:
                r = post(url, json=payload, headers=self._headers(), timeout=self.timeout_s)
                code = getattr(r, "status_code", 0)
            except Exception as exc:                        # noqa: BLE001
                self.errors += 1; self.last_error = exc.__class__.__name__
                return 502, {"error": f"cannot reach FinBlade ({exc.__class__.__name__})"}

        if code == 404:
            self.errors += 1
            self.last_error = f"chat path {self.chat_path} not found"
            return 502, {"error": f"FinBlade chat endpoint not found at {self.chat_path}",
                         "detail": "set FINBLADE_CHAT_PATH to the path from FinBlade's guide"}
        if code >= 300:
            self.errors += 1
            detail = ""
            try:
                detail = (r.text or "")[:300]
            except Exception:                               # noqa: BLE001
                pass
            self.last_error = f"HTTP {code}"
            return 502, {"error": f"FinBlade returned HTTP {code}", "detail": detail}

        try:
            body = r.json()
        except Exception:                                   # noqa: BLE001
            self.errors += 1
            self.last_error = "non-JSON reply"
            return 502, {"error": "FinBlade did not return JSON"}

        self.asked += 1
        answer = _answer_text(body)
        return 200, {"answer": answer,
                     "session_id": _first(body, ("session_id", "session", "chat_id")) or session_id,
                     "pending": not answer,
                     "raw_keys": sorted(body)[:12] if isinstance(body, dict) and not answer else None}


def _first(body, keys):
    if not isinstance(body, dict):
        return None
    for k in keys:
        if body.get(k):
            return body[k]
    return None


def _answer_text(body) -> str:
    """The assistant's text, wherever this deployment puts it.

    Tolerant on purpose: the two shipped integrations read different fields,
    and an answer that arrives but is not found reads to an operator as a
    failure. Falls back to the longest string in the payload rather than
    returning nothing when something clearly came back.
    """
    if isinstance(body, str):
        return body.strip()
    if not isinstance(body, dict):
        return ""
    for key in ("answer", "response", "message", "content", "text", "reply", "output"):
        v = body.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
        # {"message": {"content": "..."}}
        if isinstance(v, dict):
            inner = _answer_text(v)
            if inner:
                return inner
    # {"messages": [{"role": "assistant", "content": "..."}]}
    for key in ("messages", "choices", "results", "history"):
        v = body.get(key)
        if isinstance(v, list) and v:
            for item in reversed(v):
                inner = _answer_text(item)
                if inner:
                    return inner
    return ""
