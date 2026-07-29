"""Cross-camera identity service — the one process that sees every camera.

Each camera runs its own inference process with its own ByteTrack ids, so no
worker can resolve identity alone. This service owns the single
GlobalIdentityRegistry and answers "who is this?" for all of them.

PRIVACY BOUNDARY — this is the only place an embedding crosses a process
boundary. It arrives over loopback HTTP, is matched, folded into an in-memory
gallery, and dropped on TTL expiry. It is never written to the store, never
logged, and never returned to any caller. Everything leaving this service is an
opaque ``gp_`` ref. Keep it that way: the moment an embedding reaches the
database or a log file, "we hold no biometric data" stops being true.

Framework-agnostic like IngestService, so it unit-tests without FastAPI.
"""

import math
import os
import sys
import time
from typing import List, Optional, Tuple

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from finblade.appearance import TrackFeatureBank   # noqa: E402
from finblade.globalid import GlobalIdentityRegistry  # noqa: E402
from finblade.topology import CameraTopology       # noqa: E402

# Bounds on an accepted payload. A ReID vector is 512-d; the range is wide
# enough for other backbones but tight enough that a malformed or hostile
# payload cannot exhaust memory.
MIN_DIM, MAX_DIM = 32, 2048
MAX_VECTORS = 16


def validate_resolve(payload: dict) -> Tuple[bool, List[str]]:
    """Validate a resolve request. Returns (ok, errors)."""
    errors: List[str] = []
    if not isinstance(payload, dict):
        return False, ["payload is not an object"]

    cam = payload.get("camera_id")
    if not isinstance(cam, str) or not cam:
        errors.append("camera_id must be a non-empty string")

    tid = payload.get("local_track_id")
    if isinstance(tid, bool) or not isinstance(tid, int):
        errors.append("local_track_id must be an integer")

    ts = payload.get("ts")
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        errors.append("ts must be a number")

    zone = payload.get("zone_id")
    if zone is not None and not isinstance(zone, str):
        errors.append("zone_id must be a string or null")

    vectors = payload.get("embeddings")
    if not isinstance(vectors, list) or not vectors:
        errors.append("embeddings must be a non-empty list")
        return (len(errors) == 0), errors
    if len(vectors) > MAX_VECTORS:
        errors.append(f"embeddings must contain at most {MAX_VECTORS} vectors")
        return (len(errors) == 0), errors

    dim = None
    for i, vec in enumerate(vectors):
        if not isinstance(vec, list) or not vec:
            errors.append(f"embeddings[{i}] must be a non-empty list")
            continue
        if not (MIN_DIM <= len(vec) <= MAX_DIM):
            errors.append(
                f"embeddings[{i}] length {len(vec)} outside [{MIN_DIM}, {MAX_DIM}]")
            continue
        if dim is None:
            dim = len(vec)
        elif len(vec) != dim:
            errors.append(f"embeddings[{i}] dimension {len(vec)} != {dim}")
            continue
        for v in vec:
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                errors.append(f"embeddings[{i}] must contain only numbers")
                break
            if math.isnan(v) or math.isinf(v):
                # A NaN propagates into every cosine and silently poisons the
                # whole gallery — reject at the door.
                errors.append(f"embeddings[{i}] contains NaN or infinity")
                break

    return (len(errors) == 0), errors


class IdentityService:
    """Thin, validated wrapper over GlobalIdentityRegistry."""

    # Matching parameters, in precedence order: environment overrides the
    # config file, which overrides these. Kept here rather than as bare code
    # defaults so the values a deployment is actually running are inspectable
    # and changeable without editing source.
    _TUNING_DEFAULTS = {
        "threshold": 0.70,
        "margin": 0.06,
        "ttl_seconds": 300.0,
        "bank_capacity": 5,
        "max_identities": 2000,
    }
    _ENV = {
        "threshold": "FINBLADE_REID_THRESHOLD",
        "margin": "FINBLADE_REID_MARGIN",
        "ttl_seconds": "FINBLADE_REID_TTL",
    }

    def __init__(self, registry: Optional[GlobalIdentityRegistry] = None,
                 topology_path: Optional[str] = None):
        topology = CameraTopology.empty()
        self.topology_source = "default(permissive)"
        self.tuning_source = "defaults"
        tuning = dict(self._TUNING_DEFAULTS)

        raw = {}
        if topology_path and os.path.exists(topology_path):
            try:
                import yaml
                with open(topology_path, "r", encoding="utf-8") as fh:
                    raw = yaml.safe_load(fh) or {}
                topology = CameraTopology.from_dict(raw)
                self.topology_source = topology_path
            except Exception as exc:                       # noqa: BLE001
                # Never fail startup on a bad topology file: degrade to
                # permissive and make the reason visible in /stats.
                self.topology_source = f"failed({exc.__class__.__name__}) -> permissive"

        block = raw.get("matching") or {}
        if block:
            for key in tuning:
                if key in block:
                    tuning[key] = block[key]
            self.tuning_source = topology_path

        # Environment wins — it is the knob you can turn on a running container
        # without editing a file inside it.
        for key, env in self._ENV.items():
            val = os.environ.get(env)
            if val is not None:
                try:
                    tuning[key] = float(val)
                    self.tuning_source = "env"
                except ValueError:
                    pass

        # `is None`, not `or`: GlobalIdentityRegistry defines __len__, so an
        # empty registry is falsy and `registry or ...` would silently discard
        # the caller's registry (and its topology) at startup, when it is
        # always empty.
        self.registry = (registry if registry is not None else
                         GlobalIdentityRegistry(
                             topology=topology,
                             threshold=float(tuning["threshold"]),
                             margin=float(tuning["margin"]),
                             ttl_seconds=float(tuning["ttl_seconds"]),
                             bank_capacity=int(tuning["bank_capacity"]),
                             max_identities=int(tuning["max_identities"])))

    # -- GET/POST /api/v1/identity/tuning --
    def get_tuning(self) -> dict:
        r = self.registry
        return {"threshold": r.threshold, "margin": r.margin,
                "ttl_seconds": r.ttl_seconds, "bank_capacity": r.bank_capacity,
                "max_identities": r.max_identities,
                "source": self.tuning_source}

    def set_tuning(self, payload: dict) -> Tuple[int, dict]:
        """Adjust matching parameters on a RUNNING registry.

        Tuning these needs live footage, and a restart throws away the gallery
        — so you would be re-measuring from an empty state every time you
        nudged a number. Changing them in place keeps the identities already
        built and applies to the next resolve.

        Not persisted: put the value you settle on into the topology file's
        `matching:` block, or it is lost on restart.
        """
        if not isinstance(payload, dict):
            return 422, {"ok": False, "errors": ["payload must be an object"]}

        # Validate EVERYTHING before applying ANYTHING. Applying as we validate
        # meant a payload with a good threshold and a bad margin returned 422
        # having already changed the threshold — the caller is told the request
        # failed while the registry has silently moved.
        checks = (
            ("threshold", lambda v: 0.0 < v <= 1.0, "a number in (0, 1]"),
            ("margin", lambda v: v >= 0, "a number >= 0"),
            ("ttl_seconds", lambda v: v > 0, "a number > 0"),
        )
        errors = []
        applied = {}
        for key, ok, expected in checks:
            if key not in payload:
                continue
            try:
                v = float(payload[key])
            except (TypeError, ValueError):
                errors.append(f"{key} must be {expected}")
                continue
            # isfinite rejects NaN and infinity together. An infinite margin
            # passes a bare ">= 0" check and then makes every match impossible
            # — matching would stop dead with nothing in the logs to say why.
            if not math.isfinite(v) or not ok(v):
                errors.append(f"{key} must be {expected} and finite")
                continue
            applied[key] = v

        if errors:
            return 422, {"ok": False, "errors": errors}

        r = self.registry
        for key, value in applied.items():
            setattr(r, key, value)
        if not applied:
            return 422, {"ok": False,
                         "errors": ["nothing to change: send threshold, margin "
                                    "or ttl_seconds"]}
        self.tuning_source = "runtime"
        return 200, {"ok": True, "applied": applied, "current": self.get_tuning(),
                     "note": "not persisted — put it in the topology file's "
                             "matching: block to survive a restart"}

    # -- POST /api/v1/identity/resolve --
    def resolve(self, payload: dict) -> Tuple[int, dict]:
        ok, errors = validate_resolve(payload)
        if not ok:
            return 422, {"resolved": False, "errors": errors}

        bank = TrackFeatureBank(capacity=self.registry.bank_capacity)
        for vec in payload["embeddings"]:
            bank.add([float(v) for v in vec])

        result = self.registry.resolve(
            camera_id=payload["camera_id"],
            local_track_id=int(payload["local_track_id"]),
            bank=bank,
            now=float(payload["ts"]),
            zone_id=payload.get("zone_id"),
        )
        body = result.as_dict()
        body["resolved"] = True
        return 200, body

    # -- POST /api/v1/identity/release --
    def release(self, payload: dict) -> Tuple[int, dict]:
        cam = payload.get("camera_id")
        tid = payload.get("local_track_id")
        if not isinstance(cam, str) or not cam:
            return 422, {"ok": False, "errors": ["camera_id required"]}
        if isinstance(tid, bool) or not isinstance(tid, int):
            return 422, {"ok": False, "errors": ["local_track_id must be an integer"]}
        ref = self.registry.release(cam, tid)
        return 200, {"ok": True, "global_ref": ref}

    # -- GET /api/v1/identity/{ref} --
    def journey(self, global_ref: str) -> Tuple[int, dict]:
        ident = self.registry.get(global_ref)
        if ident is None:
            return 404, {"found": False, "global_ref": global_ref}
        body = ident.summary()          # summary() carries no embedding
        body["found"] = True
        return 200, body

    # -- GET /api/v1/identity/stats --
    def stats(self, now: Optional[float] = None) -> dict:
        now = time.time() if now is None else now
        self.registry.expire(now)
        snap = self.registry.snapshot()
        snap["topology_source"] = self.topology_source
        snap["threshold"] = self.registry.threshold
        snap["margin"] = self.registry.margin
        snap["ttl_seconds"] = self.registry.ttl_seconds
        return snap

    def release_camera(self, camera_id: str) -> int:
        """Drop every binding held by a camera that has gone offline."""
        if not camera_id:
            return 0
        return self.registry.release_camera(camera_id)

    # -- GET /api/v1/identity/counts --
    def counts(self, now: Optional[float] = None) -> dict:
        """People counts that need no zones defined.

        Zone occupancy answers "how many people are in this polygon", which
        requires a polygon. This answers "how many distinct people", which only
        requires identity — so it works on a camera with no zones at all.

          live         distinct people visible right now, site-wide
          unique_total distinct people seen since startup (footfall)
          per_camera   the same two numbers per camera

        per_camera unique will not sum to unique_total when someone was seen by
        more than one camera. That gap IS the de-duplication.
        """
        now = time.time() if now is None else now
        self.registry.expire(now)
        live_by_cam = self.registry.live_by_camera()
        uniq_by_cam = self.registry.unique_by_camera()
        cameras = sorted(set(live_by_cam) | set(uniq_by_cam))
        return {
            "live": self.registry.site_occupancy(),
            "unique_total": self.registry.unique_total(),
            # Cumulative, to match unique_total: sum(per_camera unique)
            # - unique_total == cross_camera. A live-scoped count here would not
            # reconcile with the other figures in the same response.
            "cross_camera": self.registry.cross_camera_total(),
            "per_camera": [
                {"camera_id": c,
                 "live": live_by_cam.get(c, 0),
                 "unique": uniq_by_cam.get(c, 0)}
                for c in cameras
            ],
            "ts": now,
        }

    # -- GET /api/v1/identity/list --
    def list_identities(self, limit: int = 200, cross_camera_only: bool = False) -> List[dict]:
        refs = (self.registry.cross_camera_refs() if cross_camera_only
                else self.registry.all_refs())
        out = []
        for ref in refs[:limit]:
            ident = self.registry.get(ref)
            if ident is not None:
                out.append(ident.summary())
        out.sort(key=lambda i: i["last_seen"], reverse=True)
        return out

    # -- POST /api/v1/identity/merge --
    def merge(self, payload: dict) -> Tuple[int, dict]:
        keep, drop = payload.get("keep_ref"), payload.get("drop_ref")
        if not isinstance(keep, str) or not isinstance(drop, str):
            return 422, {"ok": False, "errors": ["keep_ref and drop_ref required"]}
        ok = self.registry.merge(keep, drop)
        if not ok:
            return 409, {"ok": False, "error": "unknown refs or identical"}
        return 200, {"ok": True, "keep_ref": keep, "dropped_ref": drop}
