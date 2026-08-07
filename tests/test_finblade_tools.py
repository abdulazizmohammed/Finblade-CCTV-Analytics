"""Chatbot tool definitions and dispatch — Part B.

The arguments here are chosen by a model, so the tests are mostly about what
happens when it chooses badly: a null it had to send because the schema is
strict, a zone id with a slash in it, a condition given two ways at once.

No network. CCTVClient takes an injected transport.
"""

import json
import unittest

from integrations.finblade_ai.cctv_client import CCTVClient, CCTVError
from integrations.finblade_ai.chat import run_tool, run_tool_safely
from integrations.finblade_ai.tools import (SYSTEM_PROMPT, TOOL_ROUTES, TOOLS,
                                            tool_names)

NOW = 1_700_000_000.0


class FakeResponse:
    status_code = 200

    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


class Recorder:
    """Captures the request a tool call turns into."""

    def __init__(self):
        self.calls = []

    def __call__(self, path, params, **kwargs):
        self.calls.append((path, dict(params)))
        return FakeResponse({"ok": True, "path": path})

    @property
    def last(self):
        return self.calls[-1]


def client_and_recorder():
    rec = Recorder()
    return CCTVClient(base_url="http://cctv", api_key="k", get=rec), rec


class TestToolDeclarations(unittest.TestCase):
    def test_every_tool_has_a_route(self):
        self.assertEqual(set(tool_names()), set(TOOL_ROUTES))

    def test_every_schema_is_strict_and_closed(self):
        """A hallucinated field name must be a schema error at the boundary,
        not a silently empty result the model reports as 'never'."""
        for tool in TOOLS:
            with self.subTest(tool["name"]):
                self.assertTrue(tool["strict"])
                schema = tool["input_schema"]
                self.assertFalse(schema["additionalProperties"])
                # strict requires every property listed as required; the model
                # sends null for the ones it does not want.
                self.assertEqual(set(schema["properties"]), set(schema["required"]))

    def test_descriptions_say_that_null_is_not_zero(self):
        """The single most consequential thing the model can get wrong."""
        history = next(t for t in TOOLS if t["name"] == "cctv_zone_history")
        text = history["description"].lower()
        self.assertIn("not observing", text)
        self.assertIn("does not mean the zone was empty", text)

    def test_descriptions_tell_the_model_to_quote_coverage(self):
        for name in ("cctv_zone_history", "cctv_occupancy_report"):
            tool = next(t for t in TOOLS if t["name"] == name)
            self.assertIn("coverage", tool["description"].lower())

    def test_the_system_prompt_forbids_answering_who(self):
        """No identity is retained, and a chatbot implying otherwise about a
        surveillance system is a serious misstatement."""
        self.assertIn("cannot answer", SYSTEM_PROMPT.lower())
        self.assertIn("anonymous", SYSTEM_PROMPT.lower())

    def test_the_closed_sets_match_the_server(self):
        """A drift here shows up as a 422 the model cannot interpret."""
        from finblade.series import COMPARABLE_FIELDS, OPERATORS
        duration = next(t for t in TOOLS if t["name"] == "cctv_zone_duration")
        props = duration["input_schema"]["properties"]
        self.assertEqual(set(COMPARABLE_FIELDS),
                         set(props["field"]["enum"]) - {None})
        self.assertEqual(set(OPERATORS), set(props["op"]["enum"]) - {None})


class TestDispatch(unittest.TestCase):
    def setUp(self):
        self.client, self.rec = client_and_recorder()

    def test_live_state(self):
        run_tool(self.client, "cctv_live_state", {"camera_id": "CAM-01"})
        path, params = self.rec.last
        self.assertEqual("/api/v1/zones/state", path)
        self.assertEqual({"camera_id": "CAM-01"}, params)

    def test_nulls_are_dropped_not_forwarded(self):
        """Every schema is strict, so the model MUST send a value for each
        property and sends null for the ones it does not want. Forwarding those
        filters on the string "None" and returns nothing — which reads as a
        real empty answer."""
        run_tool(self.client, "cctv_live_state", {"camera_id": None})
        self.assertEqual({}, self.rec.last[1])

    def test_history_builds_the_zone_path(self):
        run_tool(self.client, "cctv_zone_history",
                 {"zone_id": "ZONE-03", "camera_id": "CAM-02", "hours": 6,
                  "from": None, "to": None, "bucket": 600})
        path, params = self.rec.last
        self.assertEqual("/api/v1/zones/ZONE-03/series", path)
        self.assertEqual(600, params["bucket"])
        self.assertEqual("CAM-02", params["camera_id"])
        self.assertAlmostEqual(6 * 3600.0, params["to"] - params["from"], places=1)

    def test_explicit_bounds_beat_hours(self):
        """A model asked about "yesterday" may send both. Guessing which it
        meant is worse than a stated precedence."""
        run_tool(self.client, "cctv_zone_history",
                 {"zone_id": "Z", "camera_id": None, "hours": 99,
                  "from": 100.0, "to": 200.0, "bucket": None})
        params = self.rec.last[1]
        self.assertEqual(100.0, params["from"])
        self.assertEqual(200.0, params["to"])

    def test_a_missing_window_falls_back_to_the_tool_default(self):
        run_tool(self.client, "cctv_zone_duration",
                 {"zone_id": "Z", "camera_id": None, "hours": None,
                  "from": None, "to": None, "field": "occupancy", "op": "gt",
                  "value": 0, "status": None})
        params = self.rec.last[1]
        self.assertAlmostEqual(24 * 3600.0, params["to"] - params["from"], places=1)

    def test_status_and_field_are_not_sent_together(self):
        """The server lets status win, which would silently ignore a condition
        the model meant to apply."""
        run_tool(self.client, "cctv_zone_duration",
                 {"zone_id": "Z", "camera_id": None, "hours": 1,
                  "from": None, "to": None, "field": "occupancy", "op": "gt",
                  "value": 3, "status": "WARNING"})
        params = self.rec.last[1]
        self.assertEqual("WARNING", params["status"])
        self.assertNotIn("field", params)
        self.assertNotIn("value", params)

    def test_a_field_condition_passes_through(self):
        run_tool(self.client, "cctv_zone_duration",
                 {"zone_id": "Z", "camera_id": None, "hours": 1,
                  "from": None, "to": None, "field": "density", "op": "gte",
                  "value": 2.0, "status": None})
        params = self.rec.last[1]
        self.assertEqual(("density", "gte", 2.0),
                         (params["field"], params["op"], params["value"]))

    def test_at_time_defaults_to_now(self):
        run_tool(self.client, "cctv_zone_at_time",
                 {"zone_id": "Z", "camera_id": None, "ts": None})
        self.assertGreater(self.rec.last[1]["ts"], 1_700_000_000.0)

    def test_alerts_and_report_go_through_the_named_allowlist(self):
        run_tool(self.client, "cctv_alerts",
                 {"hours": 2, "from": None, "to": None, "camera_id": None,
                  "zone_id": None, "rule_id": "R-06", "severity": None,
                  "status": None, "limit": None})
        self.assertEqual("/api/v1/history/alerts", self.rec.last[0])
        self.assertEqual("R-06", self.rec.last[1]["rule_id"])

        run_tool(self.client, "cctv_occupancy_report",
                 {"hours": 24, "from": None, "to": None, "camera_id": None,
                  "zone_id": None})
        self.assertEqual("/api/v1/reports/occupancy.json", self.rec.last[0])

    def test_an_unknown_tool_is_refused(self):
        with self.assertRaises(CCTVError):
            run_tool(self.client, "cctv_delete_everything", {})


class TestArgumentSafety(unittest.TestCase):
    """The zone id reaches a URL path and the model chose it."""

    def setUp(self):
        self.client, self.rec = client_and_recorder()

    def test_a_traversing_zone_id_is_refused(self):
        for bad in ("../../cameras", "a/b", "x\\y", ".."):
            with self.subTest(bad):
                with self.assertRaises(CCTVError):
                    run_tool(self.client, "cctv_zone_at_time",
                             {"zone_id": bad, "camera_id": None, "ts": NOW})
        self.assertEqual([], self.rec.calls, "nothing left the process")

    def test_the_path_allowlist_still_applies(self):
        """read_path is prefix-constrained, so even a path this module built
        wrongly cannot reach a route outside the read surface."""
        with self.assertRaises(CCTVError):
            self.client.read_path("/api/v1/cameras/CAM-01/stop")

    def test_the_zone_read_routes_are_reachable(self):
        for suffix in ("series", "at", "duration"):
            self.client.read_path(f"/api/v1/zones/ZONE-01/{suffix}", ttl=0)
        self.assertEqual(3, len(self.rec.calls))


class TestFailuresReachTheModel(unittest.TestCase):
    def test_an_upstream_failure_becomes_an_instruction_not_an_exception(self):
        """An exception ends the turn. A described failure lets the model say
        it could not reach the system — which beats answering from nothing."""
        def boom(path, params, **kwargs):
            raise ConnectionError("refused")

        client = CCTVClient(base_url="http://cctv", api_key="k", get=boom)
        out = run_tool_safely(client, "cctv_live_state", {"camera_id": None})
        self.assertIn("error", out)
        self.assertIn("do not answer from memory", out["note"])

    def test_the_result_survives_json_encoding(self):
        """Tool results are serialised into the next request; anything that
        cannot encode would fail the turn rather than the call."""
        from integrations.finblade_ai.chat import _as_text
        payload = {"coverage": None, "points": [{"occupancy": None}]}
        self.assertEqual(payload, json.loads(_as_text(payload)))


if __name__ == "__main__":
    unittest.main()
