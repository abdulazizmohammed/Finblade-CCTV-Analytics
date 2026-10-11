"""The operator-facing FinBlade chat proxy (services/api/finblade_chat.py).

The browser must never hold FinBlade's credentials, so this server signs in
and forwards. What is worth testing without FinBlade present:

  * the password never leaves the server, in any response or error;
  * a token is obtained once, reused, and re-fetched when it is rejected;
  * every failure an operator can cause — wrong password, wrong path, host
    unreachable, nothing configured — reports as itself rather than as a
    generic 500, because these are the four things that actually go wrong;
  * the answer is found wherever this FinBlade deployment puts it.

The transport is injected. Nothing here reaches the network.
"""

import os
import sys
import time
import unittest

os.environ.setdefault("FINBLADE_INMEMORY", "1")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.api.finblade_chat import FinBladeChat, MAX_QUESTION, _answer_text  # noqa: E402

PASSWORD = "s3cret-service-account-password"


class Resp:
    def __init__(self, status=200, body=None, text=""):
        self.status_code = status
        self._body = body
        self.text = text or ""

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class Transport:
    """Records what was sent and replays scripted replies."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def post(self, url, **kw):
        self.calls.append((url, kw))
        r = self.replies.pop(0) if self.replies else Resp(200, {"answer": "ok"})
        if isinstance(r, Exception):
            raise r
        return r


def client(transport=None, **over):
    # login, open a conversation, then the answer — the real order of calls.
    t = transport or Transport(Resp(200, {"auth_token": "tok-1"}),
                               Resp(200, {"id": "conv-1"}),
                               Resp(200, {"answer": "Four people are in the building."}))
    kw = dict(base_url="https://finblade.test", username="svc@wareed", password=PASSWORD,
              mcp_key="wareed-cctv", enabled=True, post=t.post, get=lambda *a, **k: None)
    kw.update(over)
    c = FinBladeChat(**kw)
    c._t = t
    return c


class TestConfiguration(unittest.TestCase):
    def test_off_until_all_three_credentials_are_present(self):
        for missing in ("base_url", "username", "password"):
            kw = {"base_url": "https://x", "username": "u", "password": "p"}
            kw[missing] = ""
            c = FinBladeChat(post=lambda *a, **k: None, **kw)
            self.assertFalse(c.configured(), missing)
            self.assertFalse(c.enabled())
            code, body = c.ask("how many people are inside?")
            self.assertEqual(503, code)
            self.assertIn("not configured", body["error"])

    def test_the_kill_switch_wins_over_being_configured(self):
        old = dict(os.environ)
        os.environ["FINBLADE_CHAT_ENABLED"] = "false"
        try:
            c = FinBladeChat(base_url="https://x", username="u", password="p",
                             post=lambda *a, **k: None)
            self.assertTrue(c.configured())
            self.assertFalse(c.enabled(), "FINBLADE_CHAT_ENABLED=false must switch it off")
        finally:
            os.environ.clear(); os.environ.update(old)

    def test_status_never_carries_the_password_or_the_token(self):
        c = client()
        s = c.status()
        blob = repr(s)
        self.assertNotIn(PASSWORD, blob)
        self.assertNotIn("tok-1", blob)
        self.assertEqual("svc@wareed", s["user"], "who it signs in as is fine to show")
        self.assertTrue(s["auth"]["ok"])
        self.assertTrue(s["auth"]["has_token"])

    def test_status_warns_when_no_mcp_service_is_named(self):
        """Without it FinBlade answers from general knowledge and the operator
        has no way to tell that from a system with nothing to report."""
        c = client(mcp_key="")
        self.assertIn("general knowledge", c.status()["warning"])
        self.assertNotIn("warning", client().status())


class TestAuth(unittest.TestCase):
    def test_the_token_is_fetched_once_and_reused(self):
        t = Transport(Resp(200, {"auth_token": "tok-1"}), Resp(200, {"id": "c1"}),
                      Resp(200, {"answer": "a"}), Resp(200, {"answer": "b"}))
        c = client(t)
        code, body = c.ask("one")
        self.assertEqual(200, code)
        self.assertEqual(200, c.ask("two", session_id=body["session_id"])[0])
        logins = [u for u, _ in t.calls if u.endswith("/token/login/")]
        self.assertEqual(1, len(logins), "signed in once for two questions")
        asked = [kw for u, kw in t.calls if u.endswith("/chat/")]
        self.assertEqual("Token tok-1", asked[0]["headers"]["Authorization"])

    def test_a_rejected_token_is_refreshed_once_then_given_up_on(self):
        # The service account's token can be revoked mid-session; one silent
        # retry is right, a loop is not.
        t = Transport(Resp(200, {"auth_token": "old"}), Resp(200, {"id": "c1"}),
                      Resp(401), Resp(200, {"auth_token": "new"}),
                      Resp(200, {"answer": "fresh"}))
        c = client(t)
        code, body = c.ask("still there?")
        self.assertEqual(200, code)
        self.assertEqual("fresh", body["answer"])
        self.assertEqual(2, len([u for u, _ in t.calls if u.endswith("/token/login/")]))

        t2 = Transport(Resp(200, {"auth_token": "old"}), Resp(200, {"id": "c1"}),
                       Resp(401), Resp(401))
        c2 = client(t2)
        code, body = c2.ask("still there?")
        self.assertEqual(502, code)
        self.assertIn("rejected the username or password", body["error"])

    def test_whatever_the_token_field_is_called(self):
        for field in ("auth_token", "token", "access", "key", "access_token"):
            t = Transport(Resp(200, {field: "T"}), Resp(200, {"id": "c"}),
                          Resp(200, {"answer": "y"}))
            self.assertEqual(200, client(t).ask("q")[0], field)
        t = Transport(Resp(200, {"detail": "ok"}))
        code, body = client(t).ask("q")
        self.assertEqual(502, code)
        self.assertIn("no token", body["error"])


class TestConversations(unittest.TestCase):
    """FinBlade answered the very first real question with HTTP 400
    "conversation_id is required". A question belongs to a conversation, and
    which path opens one differs between deployments."""

    def test_a_conversation_is_opened_and_its_id_sent_with_the_question(self):
        t = Transport(Resp(200, {"auth_token": "T"}), Resp(200, {"id": "conv-7"}),
                      Resp(200, {"answer": "a"}))
        code, body = client(t).ask("how many people are inside?")
        self.assertEqual(200, code)
        self.assertEqual("https://finblade.test/sessions/", t.calls[1][0])
        self.assertEqual("conv-7", t.calls[2][1]["json"]["conversation_id"])
        self.assertEqual("conv-7", body["session_id"], "handed back so follow-ups keep the thread")

    def test_the_second_question_reuses_the_conversation(self):
        t = Transport(Resp(200, {"auth_token": "T"}), Resp(200, {"id": "conv-7"}),
                      Resp(200, {"answer": "a"}), Resp(200, {"answer": "b"}))
        c = client(t)
        c.ask("first")
        c.ask("and yesterday?", session_id="conv-7")
        opened = [u for u, _ in t.calls if u.endswith("/sessions/")]
        self.assertEqual(1, len(opened), "one conversation for the thread")

    def test_the_sessions_path_is_discovered_and_remembered(self):
        # /sessions/ is absent on this deployment; /api/v1/sessions/ works.
        t = Transport(Resp(200, {"auth_token": "T"}), Resp(404),
                      Resp(200, {"conversation_id": "c9"}), Resp(200, {"answer": "a"}),
                      Resp(200, {"conversation_id": "c10"}), Resp(200, {"answer": "b"}))
        c = client(t)
        self.assertEqual(200, c.ask("one")[0])
        self.assertEqual("/api/v1/sessions/", c.sessions_path)
        c.ask("two")
        tried = [u for u, _ in t.calls if "sessions" in u]
        self.assertEqual(["https://finblade.test/sessions/",
                          "https://finblade.test/api/v1/sessions/",
                          "https://finblade.test/api/v1/sessions/"], tried,
                         "the dead path is not tried again")

    def test_a_strict_serialiser_that_wants_a_title(self):
        t = Transport(Resp(200, {"auth_token": "T"}), Resp(400, text="title required"),
                      Resp(200, {"id": "c1"}), Resp(200, {"answer": "a"}))
        self.assertEqual(200, client(t).ask("q")[0])
        self.assertEqual({"title": "FinBlade CCTV"}, t.calls[2][1]["json"])

    def test_when_no_conversation_can_be_opened_it_says_why(self):
        t = Transport(Resp(200, {"auth_token": "T"}), Resp(404), Resp(404))
        code, body = client(t).ask("q")
        self.assertEqual(502, code)
        self.assertIn("could not open a FinBlade conversation", body["error"])
        self.assertIn("not there", body["detail"])


class TestTheFourThingsThatGoWrong(unittest.TestCase):
    """Each reports as itself. An operator fixes the one it names."""

    def test_wrong_password(self):
        code, body = client(Transport(Resp(401))).ask("q")
        self.assertEqual(502, code)
        self.assertIn("rejected the username or password", body["error"])

    def test_base_url_with_a_path_in_it(self):
        code, body = client(Transport(Resp(404))).ask("q")
        self.assertEqual(502, code)
        self.assertIn("base URL must be the host only", body["error"])

    def test_host_unreachable(self):
        class Boom(Exception):
            pass
        Boom.__name__ = "ConnectionError"
        code, body = client(Transport(Boom())).ask("q")
        self.assertEqual(502, code)
        self.assertIn("cannot reach FinBlade", body["error"])

    def test_the_chat_path_is_wrong(self):
        t = Transport(Resp(200, {"auth_token": "T"}), Resp(200, {"id": "c1"}), Resp(404))
        code, body = client(t).ask("q")
        self.assertEqual(502, code)
        self.assertIn("chat endpoint not found", body["error"])
        self.assertIn("FINBLADE_CHAT_PATH", body["detail"])

    def test_a_slow_agent_is_pending_not_an_error(self):
        class Slow(Exception):
            pass
        Slow.__name__ = "ReadTimeout"
        t = Transport(Resp(200, {"auth_token": "T"}), Slow())
        code, body = client(t).ask("a hard question", session_id="s-9")
        self.assertEqual(504, code)
        self.assertTrue(body["pending"])
        self.assertEqual("s-9", body["session_id"], "so the UI can keep the thread")


class TestTheQuestion(unittest.TestCase):
    def test_empty_and_oversized_questions_never_reach_finblade(self):
        t = Transport()
        c = client(t)
        self.assertEqual(422, c.ask("   ")[0])
        self.assertEqual(422, c.ask("x" * (MAX_QUESTION + 1))[0])
        self.assertEqual([], t.calls, "not one request sent")

    def test_what_is_actually_sent(self):
        t = Transport(Resp(200, {"auth_token": "T"}), Resp(200, {"answer": "a"}))
        c = client(t, reasoning_effort="low")
        c.ask("who is in the lab?", session_id="s-1", context="branch RUH-HQ, camera CAM-01")
        _, kw = t.calls[-1]
        sent = kw["json"]
        self.assertEqual("who is in the lab?", sent["message"])
        self.assertEqual("wareed-cctv", sent["mcp_key"], "names OUR MCP service")
        self.assertEqual("low", sent["reasoning_effort"])
        self.assertEqual("s-1", sent["conversation_id"], "the field FinBlade asked for by name")
        self.assertEqual("branch RUH-HQ, camera CAM-01", sent["context"])
        self.assertNotIn(PASSWORD, repr(sent))

    def test_counters_move(self):
        t = Transport(Resp(200, {"auth_token": "T"}), Resp(200, {"id": "c1"}),
                      Resp(200, {"answer": "a"}), Resp(500, text="boom"))
        c = client(t)
        c.ask("one")
        self.assertEqual((1, 0), (c.asked, c.errors))
        code, body = c.ask("two", session_id="c1")
        self.assertEqual(502, code)
        self.assertEqual((1, 1), (c.asked, c.errors))
        self.assertIn("boom", body["detail"])


class TestFindingTheAnswer(unittest.TestCase):
    """FinBlade deployments put the text in different fields, and an answer
    that arrived but was not found reads to an operator as a failure."""

    def test_the_shapes_seen_in_the_wild(self):
        for body, want in (
            ({"answer": "A"}, "A"),
            ({"response": "B"}, "B"),
            ({"message": {"content": "C"}}, "C"),
            ({"messages": [{"role": "user", "content": "q"},
                           {"role": "assistant", "content": "D"}]}, "D"),
            ({"choices": [{"message": {"content": "E"}}]}, "E"),
            ("F", "F"),
        ):
            self.assertEqual(want, _answer_text(body), body)

    def test_an_unrecognised_shape_is_pending_with_its_keys_shown(self):
        """Rather than a blank bubble: the keys say what to map."""
        t = Transport(Resp(200, {"auth_token": "T"}), Resp(200, {"id": "c1"}),
                      Resp(200, {"surprise": {"nested": 1}}))
        code, body = client(t).ask("q")
        self.assertEqual(200, code)
        self.assertTrue(body["pending"])
        self.assertEqual(["surprise"], body["raw_keys"])


# --------------------------------------------------------------- HTTP routes --
try:
    from fastapi.testclient import TestClient
    from services.api.app import app
    import services.api.app as appmod
    HAVE_APP = True
except Exception:                                   # noqa: BLE001
    HAVE_APP = False


@unittest.skipUnless(HAVE_APP, "fastapi app not importable")
class TestRoutes(unittest.TestCase):
    def setUp(self):
        self.c = TestClient(app)
        self.real = appmod.chat

    def tearDown(self):
        appmod.chat = self.real

    def test_the_route_is_absent_until_it_is_configured(self):
        """404, not a broken chat: a deployment that never set this up should
        not show an operator a button that always fails."""
        appmod.chat = FinBladeChat(base_url="", username="", password="",
                                   post=lambda *a, **k: None)
        r = self.c.post("/api/v1/chat/ask", json={"question": "hello"})
        self.assertEqual(404, r.status_code)
        self.assertIn("not configured", r.json()["error"])
        self.assertFalse(self.c.get("/api/v1/chat/status").json()["enabled"])

    def test_asking_through_the_api(self):
        appmod.chat = client()
        r = self.c.post("/api/v1/chat/ask", json={"question": "how many people are inside?"})
        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual("Four people are in the building.", r.json()["answer"])
        self.assertEqual(422, self.c.post("/api/v1/chat/ask", json={}).status_code)

    def test_the_context_is_capped_so_it_cannot_become_a_payload(self):
        t = Transport(Resp(200, {"auth_token": "T"}), Resp(200, {"id": "c1"}),
                      Resp(200, {"answer": "a"}))
        appmod.chat = client(t)
        self.c.post("/api/v1/chat/ask", json={"question": "q", "context": "x" * 5000})
        self.assertEqual(500, len(t.calls[-1][1]["json"]["context"]))

    def test_the_chat_needs_the_full_key_when_auth_is_on(self):
        """It reaches everything the MCP does, including appearance search."""
        appmod.chat = client()
        old = dict(os.environ)
        os.environ["FINBLADE_API_KEY"] = "full-key-full-key"
        os.environ["FINBLADE_INTEGRATION_KEY"] = "integ-key-integ-key"
        try:
            H = {"Authorization": "Bearer integ-key-integ-key"}
            self.assertEqual(403, self.c.post("/api/v1/chat/ask", json={"question": "q"},
                                              headers=H).status_code)
            self.assertEqual(403, self.c.get("/api/v1/chat/status", headers=H).status_code)
            H = {"Authorization": "Bearer full-key-full-key"}
            self.assertEqual(200, self.c.post("/api/v1/chat/ask", json={"question": "q"},
                                              headers=H).status_code)
        finally:
            os.environ.clear(); os.environ.update(old)


if __name__ == "__main__":
    unittest.main()
