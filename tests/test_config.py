import os
import tempfile
import unittest

from finblade.config import load_camera_config

_MINIMAL = """
camera_id: CAM-X
site_id: SITE-X
source: media/clip.mp4
frame_width: 1280
frame_height: 720
model_path: models/yolov8n.pt
device: cpu
conf_threshold: 0.35
person_class_id: 0
zones:
  - zone_id: Z1
    zone_name: Lobby
    restricted: false
    capacity_max: 40
    area_sqm: 60.0
    polygon: [[0, 0], [10, 0], [10, 10]]
"""

_FULL = _MINIMAL + """
process_fps: 20
imgsz: 1280
iou: 0.5
max_det: 100
track_ttl_seconds: 3.0
offline_seconds: 15
"""


def _load(text):
    fd, path = tempfile.mkstemp(suffix=".yaml")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        return load_camera_config(path)
    finally:
        os.remove(path)


class TestConfig(unittest.TestCase):
    def test_defaults(self):
        c = _load(_MINIMAL)
        self.assertEqual(c.iou, 0.7)
        self.assertEqual(c.max_det, 300)
        self.assertEqual(c.track_ttl_seconds, 5.0)
        self.assertEqual(c.offline_seconds, 30.0)
        self.assertEqual(c.imgsz, 640)
        self.assertEqual(len(c.zones), 1)
        self.assertEqual(c.zones[0].zone_id, "Z1")

    def test_overrides(self):
        c = _load(_FULL)
        self.assertEqual(c.iou, 0.5)
        self.assertEqual(c.max_det, 100)
        self.assertEqual(c.track_ttl_seconds, 3.0)
        self.assertEqual(c.offline_seconds, 15.0)
        self.assertEqual(c.process_fps, 20)
        self.assertEqual(c.imgsz, 1280)

    def test_source_or_rtsp(self):
        c = _load(_MINIMAL.replace("source: media/clip.mp4", "rtsp_url: rtsp://x/cam1"))
        self.assertEqual(c.source, "rtsp://x/cam1")


if __name__ == "__main__":
    unittest.main()
