"""Frames reach the model as images, not as JSON (REQ-33).

The integration could already read structured CCTV data; what it could not do
was look at anything. These tests hold the two properties that make the vision
half correct: a frame is delivered as an image content block (a base64 string
serialised into JSON is just a very large piece of text the model cannot see),
and the counting path stays deterministic — the image tools are for describing
a scene, never for producing a number.
"""

import base64
import unittest

from integrations.finblade_ai import chat, tools
from integrations.finblade_ai.cctv_client import CCTVError

JPEG = b"\xff\xd8\xff\xe0dummy-jpeg-bytes"


class FakeClient:
    """Stands in for CCTVClient — no HTTP, no camera."""

    def __init__(self, frame_bytes=JPEG, incident_bytes=JPEG):
        self._frame, self._incident = frame_bytes, incident_bytes
        self.calls = []

    def frame(self, camera_id):
        self.calls.append(("frame", camera_id))
        if not self._frame:
            raise CCTVError("camera offline")
        return self._frame

    def incident_frame(self, alert_id):
        self.calls.append(("incident_frame", alert_id))
        return self._incident


class TestImageToolResults(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()

    def test_snapshot_returns_an_image_block(self):
        out = chat.run_tool(self.client, "cctv_camera_snapshot",
                            {"camera_id": "CAM-03"})
        block = chat._tool_result("toolu_1", out)
        self.assertEqual(block["type"], "tool_result")
        self.assertIsInstance(block["content"], list)
        img = block["content"][0]
        self.assertEqual(img["type"], "image")
        self.assertEqual(img["source"]["type"], "base64")
        self.assertEqual(img["source"]["media_type"], "image/jpeg")
        self.assertEqual(base64.standard_b64decode(img["source"]["data"]), JPEG)

    def test_the_image_is_not_json_encoded(self):
        """The regression that would make the whole feature pointless."""
        out = chat.run_tool(self.client, "cctv_camera_snapshot",
                            {"camera_id": "CAM-03"})
        block = chat._tool_result("toolu_1", out)
        self.assertNotIsInstance(block["content"], str,
                                 "an image serialised to a string is invisible "
                                 "to the model")

    def test_a_caption_accompanies_the_frame(self):
        out = chat.run_tool(self.client, "cctv_incident_frame",
                            {"alert_id": "42"})
        blocks = chat._tool_result("toolu_1", out)["content"]
        text = [b for b in blocks if b["type"] == "text"]
        self.assertEqual(len(text), 1)
        self.assertIn("42", text[0]["text"])

    def test_incident_frame_is_not_the_live_view(self):
        # Different endpoints entirely: for an alert raised an hour ago these
        # are different pictures, and answering with the live one would quietly
        # answer a question nobody asked.
        chat.run_tool(self.client, "cctv_incident_frame", {"alert_id": "7"})
        self.assertEqual(self.client.calls, [("incident_frame", "7")])

    def test_json_tools_are_unaffected(self):
        block = chat._tool_result("toolu_2", {"zones": [{"zone_id": "Z1"}]})
        self.assertIsInstance(block["content"], str)
        self.assertIn("Z1", block["content"])


class TestImageToolValidation(unittest.TestCase):
    def test_missing_arguments_are_rejected(self):
        client = FakeClient()
        for name, args in (("cctv_camera_snapshot", {}),
                           ("cctv_camera_snapshot", {"camera_id": ""}),
                           ("cctv_incident_frame", {}),
                           ("cctv_incident_frame", {"alert_id": 7})):
            with self.assertRaises(CCTVError, msg=f"{name} {args}"):
                chat.run_tool(client, name, args)

    def test_an_unavailable_frame_is_an_honest_failure(self):
        # Not a blank image: the model must be able to say it could not see.
        client = FakeClient(frame_bytes=b"")
        with self.assertRaises(CCTVError):
            chat.run_tool(client, "cctv_camera_snapshot", {"camera_id": "CAM-03"})
        result = chat.run_tool_safely(client, "cctv_camera_snapshot",
                                      {"camera_id": "CAM-03"})
        self.assertIn("error", result)
        self.assertIsInstance(chat._tool_result("t", result)["content"], str)


class TestToolDeclarations(unittest.TestCase):
    def _tool(self, name):
        return next(t for t in tools.TOOLS if t["name"] == name)

    def test_both_image_tools_are_declared(self):
        names = tools.tool_names()
        self.assertIn("cctv_camera_snapshot", names)
        self.assertIn("cctv_incident_frame", names)

    def test_descriptions_forbid_counting_from_the_image(self):
        """REQ-32 — the model must not become the counter.

        The determinism requirement is only as strong as what the tool
        descriptions tell the model, so it is asserted here rather than trusted.
        """
        for name in ("cctv_camera_snapshot", "cctv_incident_frame"):
            desc = self._tool(name)["description"].lower()
            self.assertIn("count", desc, name)
            self.assertTrue("do not count" in desc or "take any number" in desc,
                            f"{name} must tell the model where numbers come from")

    def test_schemas_are_strict(self):
        for name in ("cctv_camera_snapshot", "cctv_incident_frame"):
            t = self._tool(name)
            self.assertTrue(t["strict"])
            self.assertFalse(t["input_schema"]["additionalProperties"])
            self.assertTrue(t["input_schema"]["required"])


if __name__ == "__main__":
    unittest.main()
