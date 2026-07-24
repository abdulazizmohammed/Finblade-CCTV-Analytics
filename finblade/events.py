"""Event model + pure-Python schema validation (no pydantic dependency).

Six event types (UC-21..26). Each event is a plain dict with a common envelope
plus a type-specific payload. ``validate_event`` accepts a well-formed payload
and rejects a malformed one (missing field / wrong type / illegal value) with a
list of reasons — this is what the API's /events/ingest gate reuses.
"""

import uuid
from typing import List, Tuple

from .identity import PersonRefHasher

# --- event type constants -------------------------------------------------
ZONE_ENTRY = "ZONE_ENTRY"
ZONE_EXIT = "ZONE_EXIT"
ZONE_TRANSITION = "ZONE_TRANSITION"
DENSITY_UPDATE = "DENSITY_UPDATE"
CAMERA_HEARTBEAT = "CAMERA_HEARTBEAT"
CAMERA_OFFLINE = "CAMERA_OFFLINE"

EVENT_TYPES = {
    ZONE_ENTRY,
    ZONE_EXIT,
    ZONE_TRANSITION,
    DENSITY_UPDATE,
    CAMERA_HEARTBEAT,
    CAMERA_OFFLINE,
}

# Per-type required payload keys and their python types.
# ``str_or_none`` allows None for zone_from on a fresh entry etc.
_NUM = (int, float)
_SCHEMA = {
    ZONE_ENTRY: {"zone_to": str, "person_ref": str, "confidence": _NUM},
    ZONE_EXIT: {"zone_from": str, "person_ref": str},
    ZONE_TRANSITION: {"zone_from": str, "zone_to": str, "person_ref": str},
    DENSITY_UPDATE: {"zone_id": str, "occupancy": int, "density": _NUM},
    CAMERA_HEARTBEAT: {},
    CAMERA_OFFLINE: {"last_seen": _NUM},
}


def new_event(event_type: str, camera_id: str, site_id: str, ts: float, **payload) -> dict:
    """Build an event envelope. Does not validate — call validate_event for that."""
    evt = {
        "event_id": str(uuid.uuid4()),
        "event_type": event_type,
        "camera_id": camera_id,
        "site_id": site_id,
        "timestamp": ts,
    }
    evt.update(payload)
    return evt


def validate_event(evt: dict) -> Tuple[bool, List[str]]:
    """Return (ok, errors). ok is True iff errors is empty."""
    errors: List[str] = []

    if not isinstance(evt, dict):
        return False, ["event is not an object"]

    # Envelope
    et = evt.get("event_type")
    if et not in EVENT_TYPES:
        errors.append(f"unknown or missing event_type: {et!r}")
        return False, errors  # cannot check payload without a valid type

    for key, typ in (("event_id", str), ("camera_id", str), ("site_id", str)):
        v = evt.get(key)
        if not isinstance(v, str) or not v:
            errors.append(f"{key} must be a non-empty string")

    ts = evt.get("timestamp")
    if not isinstance(ts, _NUM) or isinstance(ts, bool):
        errors.append("timestamp must be a number")
    elif ts < 0:
        errors.append("timestamp must be >= 0")

    # Payload
    for field_name, expected in _SCHEMA[et].items():
        if field_name not in evt:
            errors.append(f"{et} missing required field: {field_name}")
            continue
        val = evt[field_name]
        # bool is a subclass of int; reject it where we mean a real number/str.
        if isinstance(val, bool) or not isinstance(val, expected):
            errors.append(f"{et}.{field_name} must be {expected}")

    # Value-level checks
    if et == DENSITY_UPDATE:
        occ = evt.get("occupancy")
        if isinstance(occ, int) and not isinstance(occ, bool) and occ < 0:
            errors.append("occupancy must be >= 0")
        dens = evt.get("density")
        if isinstance(dens, _NUM) and not isinstance(dens, bool) and dens < 0:
            errors.append("density must be >= 0")
    if et == ZONE_ENTRY:
        conf = evt.get("confidence")
        if isinstance(conf, _NUM) and not isinstance(conf, bool) and not (0.0 <= conf <= 1.0):
            errors.append("confidence must be in [0, 1]")

    # PII guard: any person_ref present must be an anonymous hash, never a name.
    if "person_ref" in evt and isinstance(evt["person_ref"], str):
        if not PersonRefHasher.looks_anonymous(evt["person_ref"]):
            errors.append("person_ref is not an anonymous hash (possible PII)")

    return (len(errors) == 0), errors
