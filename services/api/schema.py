"""Request payload validation for the API.

Reuses the tested finblade.events.validate_event for the ingest schema so the
API and the pipeline agree on exactly one definition of a valid event. Pure
Python — no pydantic — so it is unit-testable without installing the web stack.
"""

import os
import sys
from typing import List, Tuple

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from finblade.events import validate_event  # noqa: E402

_NUM = (int, float)

# Required fields for a 5-second zone-state aggregate (UC-29).
_ZONE_STATE_FIELDS = {
    "zone_id": str,
    "camera_id": str,
    "occupancy": int,
    "density": _NUM,
    "capacity_pct": _NUM,
    "inflow_per_min": _NUM,
    "outflow_per_min": _NUM,
    "status": str,
    "ts": _NUM,
}

_VALID_STATUS = {"NORMAL", "AMBER", "RED"}


def validate_ingest(payload: dict) -> Tuple[bool, List[str]]:
    return validate_event(payload)


def validate_zone_state(payload: dict) -> Tuple[bool, List[str]]:
    errors: List[str] = []
    if not isinstance(payload, dict):
        return False, ["zone_state is not an object"]
    for field, typ in _ZONE_STATE_FIELDS.items():
        if field not in payload:
            errors.append(f"missing field: {field}")
            continue
        v = payload[field]
        if isinstance(v, bool) or not isinstance(v, typ):
            errors.append(f"{field} must be {typ}")
    if payload.get("status") not in _VALID_STATUS:
        errors.append(f"status must be one of {sorted(_VALID_STATUS)}")
    for k in ("occupancy", "density", "capacity_pct"):
        v = payload.get(k)
        if isinstance(v, _NUM) and not isinstance(v, bool) and v < 0:
            errors.append(f"{k} must be >= 0")
    return (len(errors) == 0), errors
