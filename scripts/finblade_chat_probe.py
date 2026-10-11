#!/usr/bin/env python3
"""Find the FinBlade endpoints this deployment actually has.

    .venv/bin/python scripts/finblade_chat_probe.py

Reads FINBLADE_CHAT_* from .env, signs in with the service account, then:

  1. asks the API for a route index — most Django REST backends publish one at
     the root, which answers the question outright;
  2. failing that, probes a list of plausible conversation paths and prints
     what each one said.

Read-only apart from the sign-in and whatever a POST to a conversation
endpoint creates (an empty conversation, which is harmless). Prints no
password and no token.

Written because /sessions/ and /api/v1/sessions/ — the two paths the shipped
FinBlade integrations use — are both absent here, and guessing a third from a
desk is worse than asking the server.
"""
import json
import os
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO)

# Where a conversation might be created. Ordered by how likely the two shipped
# integrations and the chat path itself make them.
CANDIDATES = [
    "/sessions/", "/api/v1/sessions/",
    "/microservices/agent/sessions/", "/microservices/agent/conversations/",
    "/microservices/agent/conversation/", "/microservices/agent/chat/sessions/",
    "/conversations/", "/api/v1/conversations/",
    "/api/v1/chat/sessions/", "/api/v1/agent/sessions/",
    "/chat/sessions/", "/chat/conversations/",
]

# Indexes worth asking for first: a route listing makes the probe unnecessary.
INDEXES = ["/api/v1/", "/api/", "/microservices/", "/microservices/agent/", "/"]


def _env():
    p = os.path.join(REPO, ".env")
    if os.path.exists(p):
        for line in open(p):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k, v)


def short(r, n=220):
    try:
        body = r.text or ""
    except Exception:                                        # noqa: BLE001
        return ""
    body = " ".join(body.split())
    return body[:n]


def main():
    _env()
    import requests
    from services.api.finblade_chat import FinBladeChat

    c = FinBladeChat()
    if not c.configured():
        print("FINBLADE_CHAT_URL / _USER / _PASSWORD are not all set in .env")
        return 2
    print(f"base: {c.base_url}   user: {c.username}")
    ok, msg = c._login()
    if not ok:
        print(f"sign-in FAILED: {msg}")
        return 1
    print(f"sign-in ok ({c.auth_scheme} token held)\n")

    s = requests.Session()
    h = c._headers()

    print("== route indexes (a listing here answers it outright)")
    for path in INDEXES:
        try:
            r = s.get(c.base_url + path, headers=h, timeout=20)
        except Exception as exc:                             # noqa: BLE001
            print(f"  {path:34s} {exc.__class__.__name__}")
            continue
        line = f"  {path:34s} {r.status_code}"
        if r.status_code < 300:
            try:
                data = r.json()
                if isinstance(data, dict):
                    keys = sorted(data)[:25]
                    line += f"  keys: {keys}"
                    hits = [k for k in data if any(w in str(k).lower()
                            for w in ("session", "conversation", "chat", "agent"))]
                    if hits:
                        line += f"\n      -> LOOK HERE: {[(k, data[k]) for k in hits]}"
                else:
                    line += f"  {short(r, 120)}"
            except ValueError:
                line += f"  {short(r, 120)}"
        print(line)

    print("\n== conversation endpoints (GET lists, POST creates)")
    live, found = [], []
    for path in CANDIDATES:
        url = c.base_url + path
        row = f"  {path:38s}"
        try:
            g = s.get(url, headers=h, timeout=20)
            row += f" GET {g.status_code}"
        except Exception as exc:                             # noqa: BLE001
            print(row + f" GET {exc.__class__.__name__}")
            continue
        if g.status_code == 404:
            print(row)
            continue
        live.append((path, g))
        try:
            p = s.post(url, json={}, headers=h, timeout=20)
            row += f"  POST {p.status_code}  {short(p, 160)}"
            if p.status_code < 300:
                found.append((path, {}))
        except Exception as exc:                             # noqa: BLE001
            row += f"  POST {exc.__class__.__name__}"
        print(row)

    # A path that exists but rejected an empty body is the interesting one: it
    # has just said what it wants. Ask it properly rather than guessing.
    for path, g in live:
        if any(p == path for p, _ in found):
            continue
        url = c.base_url + path
        print(f"\n== {path} exists — working out what it wants")

        # What is already there says more than any guess: an existing session
        # shows the exact field values this deployment uses.
        try:
            data = g.json()
            rows = data.get("results") if isinstance(data, dict) else data
            if isinstance(rows, list) and rows:
                print(f"  existing sessions ({len(rows)}), newest shown:")
                print("   ", json.dumps(rows[0])[:400])
            else:
                print(f"  no existing sessions to copy ({short(g, 120)})")
        except ValueError:
            print(f"  GET body was not JSON: {short(g, 120)}")

        # DRF publishes its field metadata, including the allowed choices.
        choices = {}
        try:
            o = s.options(url, headers=h, timeout=20)
            meta = o.json() if o.status_code < 300 else {}
            fields = (((meta.get("actions") or {}).get("POST")) or {}) if isinstance(meta, dict) else {}
            for name, spec in (fields or {}).items():
                if isinstance(spec, dict):
                    req = spec.get("required")
                    ch = [x.get("value") for x in (spec.get("choices") or []) if isinstance(x, dict)]
                    if ch:
                        choices[name] = ch
                    print(f"  field {name:16s} type={spec.get('type')} required={req}"
                          + (f" choices={ch}" if ch else ""))
            if not fields:
                print(f"  OPTIONS gave no POST metadata ({o.status_code})")
        except Exception as exc:                             # noqa: BLE001
            print(f"  OPTIONS failed: {exc.__class__.__name__}")

        # Now try, using the choices if it gave any and a short list of
        # plausible values if it did not.
        types = choices.get("type") or ["chat", "agent", "conversation", "assistant", "default"]
        for t in types:
            body = {"title": "FinBlade CCTV", "type": t}
            try:
                p = s.post(url, json=body, headers=h, timeout=20)
            except Exception as exc:                         # noqa: BLE001
                print(f"  POST type={t!r:14s} {exc.__class__.__name__}")
                continue
            print(f"  POST type={t!r:14s} {p.status_code}  {short(p, 200)}")
            if p.status_code < 300:
                found.append((path, body))
                break

    print()
    if found:
        path, body = found[0]
        print(f"CREATES A CONVERSATION: POST {path} with {json.dumps(body)}")
        print("  put these in .env:")
        print(f"    FINBLADE_CHAT_SESSIONS_PATH={path}")
        if body:
            print(f"    FINBLADE_CHAT_SESSION_BODY={json.dumps(body)}")
    else:
        print("Still nothing created one. The route index above, or FinBlade's")
        print("integration guide, has the answer — paste this output and it is")
        print("a one-line .env change, not a code change.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
