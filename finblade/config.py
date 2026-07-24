"""Load camera + zone config from YAML into typed objects.

Kept dependency-light: only pyyaml (already installed). Zone polygons are read
verbatim from the human's config — never rewritten here.
"""

from dataclasses import dataclass
from typing import List

import yaml

from .zones import Zone, zone_from_dict


@dataclass
class CameraConfig:
    camera_id: str
    site_id: str
    source: str            # rtsp url OR file path
    frame_width: int
    frame_height: int
    model_path: str
    device: str
    conf_threshold: float
    person_class_id: int
    process_fps: int
    imgsz: int
    track_ttl_seconds: float
    zones: List[Zone]


def load_camera_config(path: str) -> CameraConfig:
    with open(path, "r") as f:
        cfg = yaml.safe_load(f)

    # Accept either 'source' (dev/file) or 'rtsp_url' (compose) — no config edit.
    source = cfg.get("source") or cfg.get("rtsp_url")

    return CameraConfig(
        camera_id=cfg.get("camera_id", "CAM-A-01"),
        site_id=cfg.get("site_id", "SITE-UNKNOWN"),
        source=source,
        frame_width=int(cfg.get("frame_width", 1280)),
        frame_height=int(cfg.get("frame_height", 720)),
        model_path=cfg.get("model_path", "/models/yolov8n.pt"),
        device=cfg.get("device", "CPU"),
        conf_threshold=float(cfg.get("conf_threshold", 0.35)),
        person_class_id=int(cfg.get("person_class_id", 0)),
        process_fps=int(cfg.get("process_fps", 12)),
        imgsz=int(cfg.get("imgsz", 640)),
        # Evict a track's per-track state this many seconds after it was last seen.
        track_ttl_seconds=float(cfg.get("track_ttl_seconds", 5.0)),
        zones=[zone_from_dict(z) for z in cfg.get("zones", [])],
    )
