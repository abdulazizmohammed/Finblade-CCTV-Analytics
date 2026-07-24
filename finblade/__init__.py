"""FinBlade CCTV crowd-analytics core.

Pure-Python, dependency-free (stdlib + optional numpy) post-detection pipeline:
geometry -> zones -> boundary debounce -> metrics -> events -> rule engine.

Everything here is deterministic and headless so it can be unit-tested without
video, YOLO, a GPU, or a network. The vision front-end (decode + YOLO + track)
feeds (track_id, x1,y1,x2,y2) tuples into this core; see services/inference.
"""

__all__ = [
    "geometry",
    "zones",
    "debounce",
    "metrics",
    "identity",
    "events",
    "rules",
    "config",
    "tracking",
]
