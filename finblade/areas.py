"""Physical areas — one real room, however many cameras watch it.

A zone polygon is one camera's view of a place. A physical area is the place
itself. Two cameras covering the same office produce two polygons, and the
person standing in the overlap is two local tracks — but one human, and the
office holds one person.

Occupancy here is therefore COUNT(DISTINCT person), never a sum of camera
counts. Summing double-counts the overlap; MAX/MIN/AVERAGE are worse, because
they are wrong in both directions at once:

    CAM-04 sees {P001, P002}      CAM-05 sees {P002, P003}
    sum = 4, max = 2              truth = |{P001, P002, P003}| = 3

Only the union of identities gives 3, so the union is what this module keeps.

WHERE THE IDENTITIES COME FROM: finblade/globalid.py already resolves each
(camera, local track) to a shared ``global_ref`` using appearance plus a
topology feasibility gate. This module does not re-implement any of that — it
consumes the refs and groups them by place.

THE REF CONTRACT — the one thing a caller must get right. Every ref passed to
observe() must identify a PERSON, not a track. When ReID has resolved a track,
that is the global_ref. When it has not, the caller must supply a fallback that
is unique per camera (e.g. "CAM-04:17"), never a bare track id: CAM-04 track 17
and CAM-05 track 17 are unrelated people, and a bare id would silently merge
them into one. See area_ref() below, which is the supported way to build it.

Unresolved refs therefore over-count rather than under-count — two views of one
person stay two until ReID links them. That is the same deliberate bias as
globalid.py: an unnecessary split is a quiet metrics error, a wrong merge puts a
stranger under someone else's identity.

Pure stdlib — no torch, no cv2, no database. Unit-testable in milliseconds.
"""

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set, Tuple

ZoneKey = Tuple[str, str]        # (camera_id, zone_id)

# A person seen in an area, then briefly seen by no camera at all, is still in
# the room. Rooms have blind spots — the gap between two cameras' fields of
# view, a pillar, a moment of occlusion — and a person walking through one has
# not left. Without a grace period the area count reads 1 -> 0 -> 1 and emits a
# spurious exit/entry pair every time somebody crosses the room.
#
# Kept short on purpose. This bridges a walk between camera views; it is not
# meant to hold someone who has actually gone. Anyone who leaves for a
# different area is removed immediately (that is a known transition, not a
# blind spot), so this delay only applies to disappearing into nowhere.
DEFAULT_LINGER_S = 4.0

# A camera that stops reporting must stop contributing. A worker that crashes
# leaves its last observation behind, and without this its people would be
# counted as present in that room indefinitely — the same failure globalid.py
# hit with orphaned bindings (see release_camera there).
DEFAULT_OBSERVATION_TTL_S = 30.0


def area_ref(camera_id: str, local_track_id, global_ref: Optional[str] = None) -> str:
    """The identity key for one tracked person, for use with observe().

    Returns the cross-camera ``global_ref`` when ReID has resolved one. Until it
    has, returns a camera-scoped fallback so two unresolved people are never
    merged by coincidence of track numbering.

    Use this rather than building the string by hand — the camera scoping is
    the part that is easy to get wrong and impossible to notice afterwards.
    """
    if global_ref:
        return str(global_ref)
    return "local:%s:%s" % (camera_id, local_track_id)


def is_resolved(ref: str) -> bool:
    """False for the camera-scoped fallback minted by area_ref()."""
    return not str(ref).startswith("local:")


@dataclass
class PhysicalArea:
    """A real place. Capacity and type belong here, not on the camera zone."""
    area_id: str
    name: str = ""
    area_type: str = "ROOM"
    capacity_max: int = 0
    area_sqm: float = 0.0
    site_id: Optional[str] = None

    def to_dict(self) -> dict:
        return {"area_id": self.area_id, "name": self.name or self.area_id,
                "area_type": self.area_type, "capacity_max": self.capacity_max,
                "area_sqm": self.area_sqm, "site_id": self.site_id}


def area_from_dict(d: dict) -> PhysicalArea:
    return PhysicalArea(
        area_id=str(d["area_id"]),
        name=str(d.get("name") or d.get("area_name") or d["area_id"]),
        area_type=str(d.get("area_type") or d.get("type") or "ROOM").upper(),
        capacity_max=int(d.get("capacity_max") or 0),
        area_sqm=float(d.get("area_sqm") or 0.0),
        site_id=d.get("site_id"),
    )


class AreaRegistry:
    """Which physical area each camera zone belongs to.

    The mapping is EXPLICIT — an operator states that CAM-04/ZONE-01 and
    CAM-05/ZONE-03 are both OFFICE-01. It is never inferred from zone names:
    "Office", "Office Room" and "Office CAM04" are three strings an operator
    might reasonably type for one room and three different rooms, and no amount
    of string matching can tell those cases apart.

    A zone with no mapping is not an error. It is the ordinary single-camera
    case, and it keeps behaving exactly as it did before areas existed.
    """

    def __init__(self, areas: Iterable[PhysicalArea] = (),
                 mapping: Optional[Dict[ZoneKey, str]] = None):
        self._areas: Dict[str, PhysicalArea] = {a.area_id: a for a in areas}
        self._zone_to_area: Dict[ZoneKey, str] = dict(mapping or {})

    # ---- definition -------------------------------------------------------
    def add_area(self, area: PhysicalArea) -> None:
        self._areas[area.area_id] = area

    def map_zone(self, camera_id: str, zone_id: str, area_id: Optional[str]) -> None:
        """Attach a camera zone to an area, or detach it when area_id is falsy."""
        key = (str(camera_id), str(zone_id))
        if not area_id:
            self._zone_to_area.pop(key, None)
            return
        area_id = str(area_id)
        self._zone_to_area[key] = area_id
        # An area referenced by a zone always exists, even if nobody described
        # it yet. Requiring definition first would make the mapping silently
        # do nothing, which is the failure mode hardest to spot from the UI.
        self._areas.setdefault(area_id, PhysicalArea(area_id=area_id, name=area_id))

    def load_zone_rows(self, rows: Iterable[dict]) -> None:
        """Apply `physical_area_id` from stored/`config` zone rows."""
        for r in rows or ():
            cam, zid = r.get("camera_id"), r.get("zone_id")
            if cam and zid:
                self.map_zone(cam, zid, r.get("physical_area_id"))

    # ---- queries ----------------------------------------------------------
    def area_of(self, camera_id: str, zone_id: str) -> Optional[str]:
        return self._zone_to_area.get((str(camera_id), str(zone_id)))

    def zones_of(self, area_id: str) -> List[ZoneKey]:
        return sorted(k for k, a in self._zone_to_area.items() if a == area_id)

    def area(self, area_id: str) -> Optional[PhysicalArea]:
        return self._areas.get(area_id)

    def areas(self) -> List[PhysicalArea]:
        return [self._areas[a] for a in sorted(self._areas)]

    def area_ids(self) -> List[str]:
        return sorted(self._areas)

    def is_multi_camera(self, area_id: str) -> bool:
        return len({cam for cam, _z in self.zones_of(area_id)}) > 1

    def __len__(self) -> int:
        return len(self._areas)


@dataclass
class _Observation:
    """One camera zone's most recent report: who it can see, and when."""
    refs: Set[str] = field(default_factory=set)
    ts: float = 0.0


class AreaOccupancy:
    """Distinct-person occupancy per physical area.

    Feed it one observation per (camera, zone) per aggregate tick; ask it for
    occupancy. It holds only refs and timestamps — no embeddings, no frames.
    """

    def __init__(self, registry: Optional[AreaRegistry] = None,
                 linger_s: float = DEFAULT_LINGER_S,
                 observation_ttl_s: float = DEFAULT_OBSERVATION_TTL_S):
        self.registry = registry if registry is not None else AreaRegistry()
        self.linger_s = float(linger_s)
        self.observation_ttl_s = float(observation_ttl_s)
        self._obs: Dict[ZoneKey, _Observation] = {}
        # ref -> (area_id, last_ts) for people not visible anywhere right now.
        # This is what bridges a blind spot without inventing an exit.
        self._lingering: Dict[str, Tuple[str, float]] = {}
        self._last_members: Dict[str, Set[str]] = {}

    # ---- input ------------------------------------------------------------
    def observe(self, camera_id: str, zone_id: str,
                refs: Iterable[str], ts: float) -> Optional[str]:
        """Record what one camera zone can see. Returns the area id, if mapped.

        ``refs`` must come from area_ref() — see the ref contract at the top of
        this module. An empty set is meaningful and must still be sent: it is
        how a camera says "this zone is empty now", as distinct from saying
        nothing at all, which means the camera has stopped reporting.
        """
        key = (str(camera_id), str(zone_id))
        refs = {str(r) for r in refs}
        self._obs[key] = _Observation(refs=refs, ts=float(ts))
        area = self.registry.area_of(camera_id, zone_id)
        # Remember where each person was last seen HERE, on the write path.
        #
        # This used to happen only in tick(), which made the blind-spot grace
        # depend on someone calling it: reads through the single-area endpoint
        # never did, so a person crossing a gap between two camera views
        # vanished from that endpoint and lingered on the list endpoint. Two
        # endpoints disagreeing about who is in a room is the kind of bug that
        # gets diagnosed as "the cameras are wrong". Recording it where the
        # observation arrives makes occupancy independent of read patterns.
        #
        # Only RESOLVED refs linger. An unresolved "local:CAM-04:17" is a
        # track, not a person: the moment ReID resolves it the ref changes to
        # a global one, and if the old key could linger it would haunt the
        # room as a second occupant for the whole window. It also cannot
        # usefully bridge anything — a camera-scoped key only ever appears on
        # the one camera, so if that camera has lost it there is nothing left
        # to reappear on.
        if area:
            for ref in refs:
                if is_resolved(ref):
                    self._lingering[ref] = (area, float(ts))
        return area

    def drop_camera(self, camera_id: str) -> int:
        """Forget every observation from one camera (it went offline).

        Also drops anyone left lingering who is not visible to some other
        camera. A camera going offline is not a blind spot — there is no
        reason to expect its people to reappear, and holding them would inflate
        the room until the window expired.
        """
        doomed = [k for k in self._obs if k[0] == str(camera_id)]
        for k in doomed:
            del self._obs[k]
        still_visible = set()
        for obs in self._obs.values():
            still_visible |= obs.refs
        for ref in [r for r in self._lingering if r not in still_visible]:
            del self._lingering[ref]
        return len(doomed)

    # ---- core computation -------------------------------------------------
    def _live_by_area(self, now: float) -> Dict[str, Set[str]]:
        """Refs each area can currently SEE, before any linger bridging."""
        out: Dict[str, Set[str]] = {}
        for (cam, zid), obs in self._obs.items():
            if (now - obs.ts) > self.observation_ttl_s:
                continue                       # camera stopped reporting
            area = self.registry.area_of(cam, zid)
            if not area:
                continue                       # unmapped zone: not an area
            out.setdefault(area, set()).update(obs.refs)
        return out

    def members(self, area_id: str, now: float) -> Set[str]:
        """The distinct people in this area right now.

        Visible people, plus anyone last seen here within the linger window who
        has not turned up somewhere else since.
        """
        live = self._live_by_area(now)
        present = set(live.get(area_id, ()))
        seen_elsewhere = set()
        for other, refs in live.items():
            if other != area_id:
                seen_elsewhere |= refs
        for ref, (where, ts) in self._lingering.items():
            if where != area_id or ref in present or ref in seen_elsewhere:
                continue
            if (now - ts) <= self.linger_s:
                present.add(ref)
        return present

    def occupancy(self, area_id: str, now: float) -> int:
        """COUNT(DISTINCT person) — the business number for this area."""
        return len(self.members(area_id, now))

    def tick(self, now: float) -> None:
        """Prune people who stopped lingering. Optional but cheap.

        Occupancy does not depend on this — observe() records positions and
        members() applies the window itself — so calling it or not cannot
        change a count. It only keeps the linger table from growing on a long
        run.
        """
        stale = [r for r, (_a, ts) in self._lingering.items()
                 if (now - ts) > self.linger_s]
        for ref in stale:
            del self._lingering[ref]

    def depart(self, ref: str, area_id: Optional[str] = None) -> bool:
        """Declare that a person has definitively left (a door crossing).

        Skips the linger grace period — a door transition is knowledge, not a
        blind spot, so the count should drop at once rather than four seconds
        later.
        """
        cur = self._lingering.get(str(ref))
        if cur is None:
            return False
        if area_id is not None and cur[0] != str(area_id):
            return False
        del self._lingering[str(ref)]
        return True

    # ---- diagnostics ------------------------------------------------------
    def camera_observations(self, area_id: str, now: float) -> List[dict]:
        """Per-camera counts, kept so an operator can see WHY a total is what
        it is. These stay valid readings — CAM-04 really does see 1 — they are
        simply not the business figure for the room."""
        out = []
        for (cam, zid), obs in sorted(self._obs.items()):
            if self.registry.area_of(cam, zid) != area_id:
                continue
            out.append({
                "camera_id": cam, "zone_id": zid,
                "observed": len(obs.refs),
                "ts": obs.ts,
                "stale": (now - obs.ts) > self.observation_ttl_s,
            })
        return out

    def state(self, area_id: str, now: float) -> dict:
        """Everything the API needs for one area."""
        area = self.registry.area(area_id) or PhysicalArea(area_id=area_id,
                                                           name=area_id)
        members = self.members(area_id, now)
        obs = self.camera_observations(area_id, now)
        summed = sum(o["observed"] for o in obs if not o["stale"])
        occ = len(members)
        cap = area.capacity_max
        mapped = self.registry.zones_of(area_id)
        # camera_count is what the operator CONFIGURED, not what happens to be
        # reporting. Deriving it from observations made a correctly mapped room
        # read "0 cam" whenever its workers were stopped, which is
        # indistinguishable from the mapping not having saved — and that is the
        # first thing anyone checks. `reporting` carries the other half.
        return {
            "area_id": area_id,
            "name": area.name or area_id,
            "area_type": area.area_type,
            "site_id": area.site_id,
            "occupancy": occ,
            "capacity_max": cap,
            "capacity_pct": round(100.0 * occ / cap, 1) if cap else 0.0,
            "density": round(occ / area.area_sqm, 3) if area.area_sqm else 0.0,
            "area_sqm": area.area_sqm,
            "camera_count": len({cam for cam, _z in mapped}),
            "zone_count": len(mapped),
            "reporting_cameras": len({o["camera_id"] for o in obs
                                      if not o["stale"]}),
            "observations": obs,
            # What a naive sum would have said. Equal to `occupancy` unless
            # cameras genuinely overlap, so it doubles as the live measure of
            # how much double-counting the area mapping is preventing.
            "summed_observations": summed,
            "double_counted": max(0, summed - occ),
            "unresolved": sum(1 for r in members if not is_resolved(r)),
        }

    def snapshot(self, now: float) -> List[dict]:
        return [self.state(a, now) for a in self.registry.area_ids()]

    # ---- area-level transitions ------------------------------------------
    def transitions(self, now: float) -> List[dict]:
        """AREA_ENTRY / AREA_EXIT since the last call.

        Derived from area membership, NOT from camera zone events. That is the
        whole point: a person walking out of CAM-04's view and into CAM-05's
        has generated a zone exit and a zone entry, but has not left the room,
        so nothing is emitted here.
        """
        out: List[dict] = []
        for area_id in self.registry.area_ids():
            now_members = self.members(area_id, now)
            was = self._last_members.get(area_id, set())
            for ref in sorted(now_members - was):
                out.append({"event": "AREA_ENTRY", "area_id": area_id,
                            "ref": ref, "ts": now})
            for ref in sorted(was - now_members):
                out.append({"event": "AREA_EXIT", "area_id": area_id,
                            "ref": ref, "ts": now})
            self._last_members[area_id] = now_members
        return out
