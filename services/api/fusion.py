"""Multi-source fusion — the entry point for detections that are not events.

Framework-agnostic like IngestService and IdentityService, so it unit-tests
without FastAPI. app.py holds one instance at module scope and its routes
delegate here.

WHAT THIS DOES TODAY (Part A). It accepts, validates and accounts for
observations (finblade/observation.py). That is the whole of it: nothing is
stored, nothing is published, nothing is fused. The endpoint is the CONTRACT,
and its job right now is to let a non-camera source — a radar — be integrated,
posted to, and seen to be arriving, before any fusion exists to consume it.

That ordering is deliberate rather than lazy. A radar carries position and
velocity and no appearance channel at all, so it can never be identity-matched
the way two cameras are (finblade/appearance.py + finblade/globalid.py). The
only way its detections can be tied to a camera's is geometrically, on a shared
ground plane — which does not exist yet and must not be built against guessed
constants. So the seam ships first and the fusion follows it.

WHAT IT DELIBERATELY DOES NOT DO YET:

  * No persistence. Storing observations needs a table, and the shape of that
    table depends on what fusion turns out to need. An empty table with the
    wrong columns is worse than no table.
  * No bus publication. Merged counts onto a stream is Part C, and it belongs
    on the facility roster's numbers, not on raw per-source detections — which
    is the whole point of counting on global identity rather than on detections.
  * No geometry. Part B.

WHY THE COUNTERS ARE THE DELIVERABLE. This project's standing problem is that
nobody can see whether the vision path is doing anything (CLAUDE.md, PRIME
DIRECTIVE). A source that posts nothing looks exactly like a quiet building, and
a source posting malformed payloads that are silently dropped looks exactly like
a source posting nothing. So every observation lands in a per-source tally,
accepted and rejected separately, with the last few payloads kept for
inspection. That is what makes "is the radar actually wired up" answerable
without a fusion path to observe it through.
"""

import time
from collections import deque
from typing import Deque, Dict, List, Optional, Tuple

from finblade.observation import (
    APPEARANCE_CAPABLE, SOURCE_TYPES, can_appearance_match, validate_observation,
)

# Recent observations retained per source, for shakeout inspection. Bounded so a
# chatty source cannot grow this without limit; small because its only purpose
# is "show me what is arriving", not history.
_RECENT_PER_SOURCE = 50

# A source with no traffic for this long is reported as silent. Not an alert —
# camera liveness is already R-07's job via /api/v1/cameras/health, and
# duplicating it here would produce two answers that can disagree. This is
# purely descriptive.
_SILENT_AFTER_S = 60.0


class FusionService:
    """Accepts source-agnostic observations and accounts for them per source."""

    def __init__(self, recent_per_source: int = _RECENT_PER_SOURCE,
                 silent_after_s: float = _SILENT_AFTER_S):
        self.recent_per_source = int(recent_per_source)
        self.silent_after_s = float(silent_after_s)
        # source_id -> tallies. Keyed on source_id rather than (type, id)
        # because an id that changes sensor type is a misconfiguration we want
        # to see as a conflict, not to silently split into two rows.
        self._sources: Dict[str, dict] = {}
        self._recent: Dict[str, Deque[dict]] = {}
        self.stats = {"accepted": 0, "rejected": 0}

    # -- POST /api/v1/observations/ingest --
    def ingest_observation(self, payload: dict) -> Tuple[int, dict]:
        ok, errors = validate_observation(payload)
        if not ok:
            # Tally the rejection against the source when the payload named one
            # legibly. A malformed payload from a known source is the case worth
            # seeing; one with no usable source_id has nowhere to go but the
            # global counter.
            sid = payload.get("source_id") if isinstance(payload, dict) else None
            if isinstance(sid, str) and sid:
                entry = self._sources.get(sid)
                if entry is not None:
                    entry["rejected"] += 1
            self.stats["rejected"] += 1
            return 422, {"accepted": False, "errors": errors}

        sid = payload["source_id"]
        stype = payload["source_type"]
        ts = float(payload["ts"])

        entry = self._sources.get(sid)
        if entry is None:
            entry = self._sources[sid] = {
                "source_id": sid,
                "source_type": stype,
                "site_id": payload["site_id"],
                "first_seen": ts,
                "last_seen": ts,
                "accepted": 0,
                "rejected": 0,
                "appearance_capable": can_appearance_match(payload),
                "frames": {},          # IMAGE/SITE -> count
                "classes": {},         # object_class -> count
                "type_conflicts": 0,
            }
            self._recent[sid] = deque(maxlen=self.recent_per_source)
        elif entry["source_type"] != stype:
            # One id, two sensor types. Not rejected — the observation itself is
            # well-formed and dropping it would lose real data — but counted,
            # because it means two publishers were configured with the same id
            # and every per-source number here is now a blend of both.
            entry["type_conflicts"] += 1

        entry["accepted"] += 1
        entry["last_seen"] = max(entry["last_seen"], ts)
        frame = payload["position"]["frame"]
        entry["frames"][frame] = entry["frames"].get(frame, 0) + 1
        oc = payload["object_class"]
        entry["classes"][oc] = entry["classes"].get(oc, 0) + 1

        self._recent[sid].append(payload)
        self.stats["accepted"] += 1
        return 202, {"accepted": True,
                     "observation_id": payload["observation_id"],
                     # Echoed so a new publisher can confirm from its own logs
                     # which fusion paths it is eligible for, without reading
                     # this source. A radar seeing false here is correct.
                     "appearance_capable": can_appearance_match(payload),
                     "fusable": False,
                     "fusable_reason": "no ground-plane calibration configured"}

    # -- GET /api/v1/observations/sources --
    def sources(self, now: Optional[float] = None) -> List[dict]:
        """Per-source tallies, newest traffic first."""
        now = time.time() if now is None else now
        out = []
        for entry in self._sources.values():
            row = dict(entry)
            row["frames"] = dict(entry["frames"])
            row["classes"] = dict(entry["classes"])
            row["silent_for"] = max(0.0, now - entry["last_seen"])
            row["silent"] = row["silent_for"] > self.silent_after_s
            out.append(row)
        out.sort(key=lambda r: r["last_seen"], reverse=True)
        return out

    # -- GET /api/v1/observations/stats --
    def snapshot(self, now: Optional[float] = None) -> dict:
        now = time.time() if now is None else now
        rows = self.sources(now)
        return {
            "accepted": self.stats["accepted"],
            "rejected": self.stats["rejected"],
            "sources": rows,
            "source_count": len(rows),
            "silent_sources": [r["source_id"] for r in rows if r["silent"]],
            "known_source_types": sorted(SOURCE_TYPES),
            "appearance_capable_types": sorted(APPEARANCE_CAPABLE),
            # Stated rather than implied, so this reads as a deliberate stage
            # and not as a fusion path that is failing silently.
            "fusion": {
                "geometric": False,
                "reason": "ground-plane calibration not implemented (Part B)",
            },
        }

    # -- GET /api/v1/observations/recent --
    def recent(self, source_id: Optional[str] = None,
               limit: int = 20) -> List[dict]:
        """The last few accepted observations, for shakeout inspection."""
        limit = max(1, min(int(limit), self.recent_per_source * 4))
        if source_id is not None:
            return list(self._recent.get(source_id, ()))[-limit:]
        merged: List[dict] = []
        for buf in self._recent.values():
            merged.extend(buf)
        merged.sort(key=lambda o: o["ts"], reverse=True)
        return merged[:limit]
