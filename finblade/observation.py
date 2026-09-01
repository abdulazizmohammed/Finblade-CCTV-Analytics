"""Source-agnostic detection observations — the seam a non-camera source plugs into.

An OBSERVATION is one detection, from one source, at one instant, before any
interpretation. It is deliberately NOT an event: `finblade/events.py` describes
things that have already been decided ("this person entered ZONE-03"), and every
one of its types is keyed on `camera_id` and a zone id. An observation describes
only what was measured — a position, a class, a confidence — and says which kind
of sensor measured it.

WHY THIS EXISTS. Today a camera is the only thing that can produce input,
because the pipeline's entry point is an event that already assumes a camera
resolved a zone. A radar produces positions and velocities and knows nothing
about polygons or appearance; there is no shape in this codebase it can post.
This module is that shape.

THE DISCRIMINATOR IS ``position.frame``, and it is what makes the seam real
rather than aspirational:

    IMAGE   pixel coordinates in one source's own frame. Meaningless to anyone
            else without that source's calibration. This is what a camera can
            emit TODAY, with no calibration work at all.
    SITE    metres on the site ground plane, shared by every source. This is
            what a radar emits natively, and what a camera emits once it has a
            homography (Part B).

Without that distinction, a shared schema would force every source to be
calibrated before any source could publish — which would make ground-plane
calibration a prerequisite for the schema rather than the other way round. With
it, an uncalibrated camera participates in everything except geometric fusion.

WHAT THIS IS NOT. It does not replace `finblade/events.py`. The 17 event types
remain the downstream contract: they are in the Postgres schema, the FinBlade
forwarder spec, the dashboard and most of the test suite. A radar source
publishes observations, and its output becomes the SAME ZONE_ENTRY /
FACILITY_ENTRY events any camera produces. `source_id` supplements `camera_id`;
it does not rename it.

PRIVACY. An observation may be persisted and forwarded, so it never carries an
appearance vector. `signature` records only that a descriptor exists and what
kind it is. Embeddings continue to travel on exactly one path —
POST /api/v1/identity/resolve, held in RAM by services/api/identity.py and
dropped on TTL. Putting a vector in an observation would move biometric data
into the database, and "we hold no biometric data" would stop being true. The
validator below rejects that outright rather than trusting callers.

Pure stdlib, no numpy, no yaml, no cv2 — testable headless like the rest of
finblade/.
"""

import uuid
from typing import List, Optional, Sequence, Tuple

from .geometry import foot_point

# --- vocabularies ---------------------------------------------------------
#
# These are CLOSED SETS, validated. A free-form string would let a typo
# ("RADR", "Person") create a phantom source or class that silently never
# matches any downstream filter, and nothing would report an error. Adding a
# real new source means adding one line here — a declaration, not logic, and
# the only downstream change a new publisher requires.
CAMERA = "CAMERA"
RADAR = "RADAR"
LIDAR = "LIDAR"

SOURCE_TYPES = {CAMERA, RADAR, LIDAR}

# Which sources can carry an appearance descriptor at all. A radar measures
# position and velocity and has no visual channel, so it can never be identity-
# matched by appearance — only geometrically, on the shared ground plane. That
# is a property of the sensor, not a configuration choice, so it belongs here
# where the fusion path can consult it rather than in a YAML file where it could
# be set wrongly.
APPEARANCE_CAPABLE = {CAMERA}

# Frames of reference for a position. See the module docstring.
FRAME_IMAGE = "IMAGE"
FRAME_SITE = "SITE"
FRAMES = {FRAME_IMAGE, FRAME_SITE}

# Only PERSON is consumed downstream today — the detector runs COCO class 0 and
# every rule, zone and roster counts people. The others are here because a radar
# does not get to choose what walks or drives past it, and an observation it
# cannot express is an observation that gets silently dropped or, worse,
# mislabelled as a person.
PERSON = "PERSON"
VEHICLE = "VEHICLE"
BICYCLE = "BICYCLE"
UNKNOWN = "UNKNOWN"

OBJECT_CLASSES = {PERSON, VEHICLE, BICYCLE, UNKNOWN}

# Keys that must never appear inside `signature`. See the privacy note above.
_FORBIDDEN_SIGNATURE_KEYS = {"vector", "vectors", "embedding", "embeddings",
                             "features", "descriptor"}

_NUM = (int, float)


def _is_num(v) -> bool:
    """A real number. bool is a subclass of int and is never meant here."""
    return isinstance(v, _NUM) and not isinstance(v, bool)


def _is_finite(v) -> bool:
    """Reject NaN and infinity.

    Same reasoning as the embedding validator in services/api/identity.py: a NaN
    coordinate does not fail, it propagates. It would survive every comparison
    in a distance-based merge, sit inside every radius, and quietly fuse
    unrelated tracks. Refuse it at the door where the error is still legible.
    """
    return v == v and v not in (float("inf"), float("-inf"))


def looks_anonymous_global_ref(ref: str) -> bool:
    """Sanity gate for a cross-camera identity ref.

    Mirrors ``PersonRefHasher.looks_anonymous`` for the ``gp_`` refs minted by
    ``GlobalIdentityRegistry._mint_ref`` (globalid.py) — an opaque salted hash
    that reverses to nothing. Kept here rather than imported so this module
    stays free of the identity subsystem; tests/test_observation.py asserts a
    freshly minted registry ref passes this, so the two cannot drift apart
    unnoticed.
    """
    if not isinstance(ref, str) or not ref.startswith("gp_"):
        return False
    body = ref[3:]
    return len(body) == 16 and all(c in "0123456789abcdef" for c in body)


# --- construction ---------------------------------------------------------
def new_observation(source_type: str, source_id: str, site_id: str, ts: float,
                    x: float, y: float, frame: str = FRAME_IMAGE,
                    object_class: str = PERSON, confidence: float = 1.0,
                    **extra) -> dict:
    """Build an observation envelope. Does not validate — call validate_observation.

    Mirrors ``events.new_event``: cheap, total, and never the thing that decides
    whether a payload is acceptable.
    """
    obs = {
        "observation_id": str(uuid.uuid4()),
        "source_type": source_type,
        "source_id": source_id,
        "site_id": site_id,
        "ts": ts,
        "object_class": object_class,
        "confidence": confidence,
        "position": {"frame": frame, "x": x, "y": y, "z": None,
                     "accuracy_m": None},
    }
    # Position sub-fields are addressable at the top level for convenience, so a
    # caller does not have to rebuild the nested dict to set an accuracy.
    for key in ("z", "accuracy_m"):
        if key in extra:
            obs["position"][key] = extra.pop(key)
    obs.update(extra)
    return obs


def observation_from_bbox(source_id: str, site_id: str, ts: float,
                          bbox: Sequence[float], confidence: float,
                          local_track_id: Optional[int] = None,
                          zone_id: Optional[str] = None,
                          global_ref: Optional[str] = None,
                          object_class: str = PERSON) -> dict:
    """Camera observation from a detector box, positioned on the FOOT POINT.

    The foot point, not the centroid — the same choice zone assignment already
    makes in finblade/geometry.py, and for the same reason: it is where the
    person is standing. It is also the only point on a bounding box that a
    ground-plane homography can map correctly, since a homography maps a plane
    and the feet are the only part of a person on it. Positioning on the
    centroid now would silently make Part B wrong later.
    """
    x1, y1, x2, y2 = (float(v) for v in bbox)
    fx, fy = foot_point(x1, y1, x2, y2)
    obs = new_observation(CAMERA, source_id, site_id, ts, fx, fy,
                          frame=FRAME_IMAGE, object_class=object_class,
                          confidence=confidence)
    obs["bbox"] = [x1, y1, x2, y2]
    if local_track_id is not None:
        obs["local_track_id"] = int(local_track_id)
    if zone_id is not None:
        obs["zone_id"] = zone_id
    if global_ref is not None:
        obs["global_ref"] = global_ref
    return obs


# --- validation -----------------------------------------------------------
def _validate_position(pos, errors: List[str]) -> Optional[str]:
    """Validate the position block. Returns the frame if it is usable."""
    if not isinstance(pos, dict):
        errors.append("position must be an object")
        return None

    frame = pos.get("frame")
    if frame not in FRAMES:
        errors.append(f"position.frame must be one of {sorted(FRAMES)}")
        frame = None

    for axis in ("x", "y"):
        v = pos.get(axis)
        if not _is_num(v):
            errors.append(f"position.{axis} must be a number")
        elif not _is_finite(v):
            errors.append(f"position.{axis} must be finite")

    # z is the height above the plane. Optional everywhere: a camera on a single
    # ground plane has no useful value for it, and a radar may or may not.
    z = pos.get("z")
    if z is not None:
        if not _is_num(z):
            errors.append("position.z must be a number or null")
        elif not _is_finite(z):
            errors.append("position.z must be finite")

    # How wrong this position might be, in metres. A radar reports it; a camera
    # can only estimate it. It is optional rather than required because a source
    # that does not know its own error should say nothing rather than invent a
    # number that a downstream merge would then trust.
    acc = pos.get("accuracy_m")
    if acc is not None:
        if not _is_num(acc):
            errors.append("position.accuracy_m must be a number or null")
        elif not _is_finite(acc):
            errors.append("position.accuracy_m must be finite")
        elif acc < 0:
            errors.append("position.accuracy_m must be >= 0")
    if frame == FRAME_IMAGE and acc is not None:
        # Pixels are not metres, and no conversion exists without a homography.
        # Accepting this would produce a number that looks like a tolerance and
        # is not one.
        errors.append("position.accuracy_m is meaningless in the IMAGE frame; "
                      "it requires SITE coordinates")
    return frame


def _validate_velocity(vel, frame: Optional[str], errors: List[str]) -> None:
    """Validate the optional velocity block.

    Carried because it is the one thing a radar measures directly that a camera
    does not, and because it is the natural gate for a future fusion path: a
    contact moving at 12 m/s is not a person walking, whatever its class says.
    Nothing consumes it yet.
    """
    if not isinstance(vel, dict):
        errors.append("velocity must be an object")
        return
    for axis in ("vx", "vy"):
        v = vel.get(axis)
        if v is None:
            errors.append(f"velocity.{axis} is required when velocity is present")
        elif not _is_num(v):
            errors.append(f"velocity.{axis} must be a number")
        elif not _is_finite(v):
            errors.append(f"velocity.{axis} must be finite")
    speed = vel.get("speed_mps")
    if speed is not None:
        if not _is_num(speed):
            errors.append("velocity.speed_mps must be a number or null")
        elif not _is_finite(speed):
            errors.append("velocity.speed_mps must be finite")
        elif speed < 0:
            errors.append("velocity.speed_mps must be >= 0")
    # Velocity in pixels per second is not a speed and cannot be compared across
    # sources. Same reasoning as accuracy_m.
    if frame == FRAME_IMAGE:
        errors.append("velocity requires SITE coordinates; it is not "
                      "expressible in the IMAGE frame")


def _validate_signature(sig, errors: List[str]) -> None:
    """Validate the optional appearance-descriptor METADATA.

    Records that a descriptor exists and what produced it, so a fusion path can
    tell "this source has no appearance channel" from "this source has one and
    it has not been sampled yet". It never carries the descriptor itself.
    """
    if not isinstance(sig, dict):
        errors.append("signature must be an object")
        return
    leaked = _FORBIDDEN_SIGNATURE_KEYS & set(sig)
    if leaked:
        errors.append(
            "signature must not contain appearance data (found: "
            f"{', '.join(sorted(leaked))}); embeddings travel only on "
            "/api/v1/identity/resolve and are never persisted")
    kind = sig.get("kind")
    if not isinstance(kind, str) or not kind:
        errors.append("signature.kind must be a non-empty string")
    dim = sig.get("dim")
    if dim is not None and (isinstance(dim, bool) or not isinstance(dim, int)
                            or dim <= 0):
        errors.append("signature.dim must be a positive integer")


def validate_observation(obs: dict) -> Tuple[bool, List[str]]:
    """Return (ok, errors). ok is True iff errors is empty.

    Same contract and style as ``events.validate_event`` — hand-written, no
    pydantic, so it runs in a test suite with no web stack installed.
    """
    errors: List[str] = []

    if not isinstance(obs, dict):
        return False, ["observation is not an object"]

    for key in ("observation_id", "source_id", "site_id"):
        v = obs.get(key)
        if not isinstance(v, str) or not v:
            errors.append(f"{key} must be a non-empty string")

    st = obs.get("source_type")
    if st not in SOURCE_TYPES:
        errors.append(f"source_type must be one of {sorted(SOURCE_TYPES)}")

    oc = obs.get("object_class")
    if oc not in OBJECT_CLASSES:
        errors.append(f"object_class must be one of {sorted(OBJECT_CLASSES)}")

    ts = obs.get("ts")
    if not _is_num(ts):
        errors.append("ts must be a number")
    elif not _is_finite(ts):
        errors.append("ts must be finite")
    elif ts < 0:
        errors.append("ts must be >= 0")

    conf = obs.get("confidence")
    if not _is_num(conf):
        errors.append("confidence must be a number")
    elif not _is_finite(conf) or not (0.0 <= conf <= 1.0):
        errors.append("confidence must be in [0, 1]")

    frame = _validate_position(obs.get("position"), errors)

    # --- optional fields, checked only when present ---
    if "local_track_id" in obs:
        tid = obs["local_track_id"]
        if isinstance(tid, bool) or not isinstance(tid, int):
            errors.append("local_track_id must be an integer")
        elif tid < 0:
            errors.append("local_track_id must be >= 0")

    if "zone_id" in obs and obs["zone_id"] is not None:
        if not isinstance(obs["zone_id"], str) or not obs["zone_id"]:
            errors.append("zone_id must be a non-empty string or null")

    if "global_ref" in obs and obs["global_ref"] is not None:
        if not looks_anonymous_global_ref(obs["global_ref"]):
            errors.append("global_ref is not an anonymous gp_ hash (possible PII)")

    if "bbox" in obs:
        bbox = obs["bbox"]
        if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
            errors.append("bbox must be [x1, y1, x2, y2]")
        elif not all(_is_num(v) and _is_finite(v) for v in bbox):
            errors.append("bbox must contain four finite numbers")
        elif bbox[2] < bbox[0] or bbox[3] < bbox[1]:
            errors.append("bbox must satisfy x1 <= x2 and y1 <= y2")
        elif frame == FRAME_SITE:
            # A bbox is pixels in one source's frame. Carrying it alongside a
            # site-plane position invites a consumer to mix the two coordinate
            # systems, which is the single easiest way to get fusion wrong.
            errors.append("bbox belongs to the IMAGE frame; a SITE-frame "
                          "observation must not carry one")

    if "velocity" in obs and obs["velocity"] is not None:
        _validate_velocity(obs["velocity"], frame, errors)

    if "signature" in obs and obs["signature"] is not None:
        _validate_signature(obs["signature"], errors)
        # A source with no visual channel cannot have produced a descriptor.
        # This catches a misconfigured publisher claiming an appearance it does
        # not have, which would otherwise enter identity matching as evidence.
        if st in SOURCE_TYPES and st not in APPEARANCE_CAPABLE:
            errors.append(f"{st} has no appearance channel and must not carry "
                          "a signature")

    return (len(errors) == 0), errors


def can_appearance_match(obs: dict) -> bool:
    """Whether this observation's source could ever be matched on appearance.

    False for radar and lidar. Such a source can only be fused geometrically,
    on the shared ground plane — which is why ground-plane calibration is a
    prerequisite for a radar source taking part in identity, not an optional
    improvement to it.
    """
    return obs.get("source_type") in APPEARANCE_CAPABLE
