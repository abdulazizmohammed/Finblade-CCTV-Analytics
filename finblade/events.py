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
CAPACITY_WARNING = "CAPACITY_WARNING"
RESTRICTED_ZONE_ENTRY = "RESTRICTED_ZONE_ENTRY"
RESTRICTED_ZONE_EXIT = "RESTRICTED_ZONE_EXIT"
LOITERING_START = "LOITERING_START"
LOITERING_END = "LOITERING_END"
CAMERA_HEARTBEAT = "CAMERA_HEARTBEAT"
CAMERA_ONLINE = "CAMERA_ONLINE"
CAMERA_OFFLINE = "CAMERA_OFFLINE"
CAMERA_RECOVERED = "CAMERA_RECOVERED"

EVENT_TYPES = {
    ZONE_ENTRY, ZONE_EXIT, ZONE_TRANSITION, DENSITY_UPDATE, CAPACITY_WARNING,
    RESTRICTED_ZONE_ENTRY, RESTRICTED_ZONE_EXIT, LOITERING_START, LOITERING_END,
    CAMERA_HEARTBEAT, CAMERA_ONLINE, CAMERA_OFFLINE, CAMERA_RECOVERED,
}

# Per-type required payload keys and their python types.
_NUM = (int, float)
_SCHEMA = {
    ZONE_ENTRY: {"zone_to": str, "person_ref": str, "confidence": _NUM},
    ZONE_EXIT: {"zone_from": str, "person_ref": str},
    ZONE_TRANSITION: {"zone_from": str, "zone_to": str, "person_ref": str},
    DENSITY_UPDATE: {"zone_id": str, "occupancy": int, "density": _NUM},
    CAPACITY_WARNING: {"zone_id": str, "occupancy": int, "capacity_pct": _NUM},
    RESTRICTED_ZONE_ENTRY: {"zone_id": str, "person_ref": str},
    RESTRICTED_ZONE_EXIT: {"zone_id": str, "person_ref": str, "duration": _NUM},
    LOITERING_START: {"zone_id": str, "person_ref": str, "dwell_time": _NUM},
    LOITERING_END: {"zone_id": str, "person_ref": str, "dwell_time": _NUM},
    CAMERA_HEARTBEAT: {},
    CAMERA_ONLINE: {},
    CAMERA_OFFLINE: {"last_seen": _NUM},
    CAMERA_RECOVERED: {},
}

# Fields that are type-checked WHEN PRESENT but never required.
#
# The movement events carry the zone occupancy that resulted from them, so a
# consumer can reconstruct occupancy over time from the event stream alone —
# "after this entry, ZONE-01 held 4 people". Today that number exists only in
# DENSITY_UPDATE, which is emitted on a 5-second cadence and duplicates
# zone_state_ts row for row; the plan is to stop emitting it, and without these
# fields that would leave the series unreconstructable.
#
# Optional rather than required on purpose: an older camera worker that does not
# send them must keep validating, or upgrading the API ahead of the workers
# rejects every event they post.
_OPTIONAL_SCHEMA = {
    # occupancy/density of the zone the person is now IN.
    ZONE_ENTRY: {"occupancy": int, "density": _NUM},
    # ...of the zone they just LEFT.
    ZONE_EXIT: {"occupancy": int, "density": _NUM},
    # ...of zone_to, plus the origin zone under _from. A transition changes two
    # zones at once, so one pair of numbers cannot describe it.
    ZONE_TRANSITION: {"occupancy": int, "density": _NUM,
                      "occupancy_from": int, "density_from": _NUM},
    # Occupancy per zone at the moment the camera came up. Without this, a
    # restart leaves every zone's count unknown until someone next moves —
    # which could be hours.
    CAMERA_ONLINE: {"zone_occupancy": dict},
    CAMERA_RECOVERED: {"zone_occupancy": dict},
}

_NON_NEGATIVE = ("occupancy", "density", "occupancy_from", "density_from")


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

    # Optional payload: checked only when present.
    for field_name, expected in _OPTIONAL_SCHEMA.get(et, {}).items():
        if field_name not in evt:
            continue
        val = evt[field_name]
        if isinstance(val, bool) or not isinstance(val, expected):
            errors.append(f"{et}.{field_name} must be {expected}")

    # Value-level checks. Applied to every count/density field on any event, so
    # a new optional field cannot be added without inheriting the >= 0 rule.
    for field_name in _NON_NEGATIVE:
        val = evt.get(field_name)
        if isinstance(val, _NUM) and not isinstance(val, bool) and val < 0:
            errors.append(f"{field_name} must be >= 0")
    if et in (CAMERA_ONLINE, CAMERA_RECOVERED):
        zo = evt.get("zone_occupancy")
        if isinstance(zo, dict):
            for zone_id, occ in zo.items():
                if not isinstance(zone_id, str) or not zone_id:
                    errors.append("zone_occupancy keys must be non-empty strings")
                if isinstance(occ, bool) or not isinstance(occ, int) or occ < 0:
                    errors.append(f"zone_occupancy[{zone_id!r}] must be an int >= 0")
    if et == ZONE_ENTRY:
        conf = evt.get("confidence")
        if isinstance(conf, _NUM) and not isinstance(conf, bool) and not (0.0 <= conf <= 1.0):
            errors.append("confidence must be in [0, 1]")

    # PII guard: any person_ref present must be an anonymous hash, never a name.
    if "person_ref" in evt and isinstance(evt["person_ref"], str):
        if not PersonRefHasher.looks_anonymous(evt["person_ref"]):
            errors.append("person_ref is not an anonymous hash (possible PII)")

    return (len(errors) == 0), errors
