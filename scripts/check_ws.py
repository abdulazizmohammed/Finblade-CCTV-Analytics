"""One WebSocket frame from the running API, as a tester would see it."""
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

KEY = open(os.path.join(os.path.dirname(__file__), "..",
                        ".local_integration_key")).read().strip()
PORT = os.environ.get("PORT", "8000")

try:
    from websockets.sync.client import connect
except Exception:
    print("websockets not installed — use the dashboard, which opens /ws itself,")
    print("or:  .venv/bin/pip install websockets")
    raise SystemExit(0)

with connect(f"ws://127.0.0.1:{PORT}/ws?key={KEY}") as ws:
    frame = json.loads(ws.recv())

print("frame keys :", sorted(frame))
print("bytes      :", len(json.dumps(frame)))
print("cameras    :", len(frame["cameras"]), "| zones:", len(frame["zones"]),
      "| alerts:", len(frame["alerts"]))
online = [c for c in frame["cameras"] if c.get("effective_state") == "ONLINE"]
for c in online:
    print("  ONLINE %s people_in_view=%s fps=%s" % (
        c["camera_id"], c.get("people_in_view"), c.get("input_fps")))
print("source field leaked to this key:",
      any("source" in c for c in frame["cameras"]))
print("credential anywhere in frame   :",
      "rtsp://" in json.dumps(frame) and "@" in json.dumps(frame))

try:
    with connect(f"ws://127.0.0.1:{PORT}/ws") as ws:
        ws.recv()
    print("no-key connect : ACCEPTED  <-- would be a hole")
except Exception as exc:
    print("no-key connect : rejected (%s)" % type(exc).__name__)
