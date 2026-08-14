"""Facility presence roster — occupancy that survives a person leaving camera view.

Zone occupancy answers "how many foot points are inside this polygon on this
frame". It is derived fresh every frame, so it is self-correcting: a wrong count
is repaired by the next frame. It is also, for the same reason, blind — the
moment someone walks into a corridor, a meeting room or a stairwell with no
camera, they stop existing.

A facility roster answers a different question: "how many people are inside the
building". Someone in an unmonitored corridor is still inside it. The two
numbers are not interchangeable and must not be conflated on a dashboard:

  ZONE OCCUPANCY   derived per frame  self-correcting   blind to dead zones
  FACILITY ROSTER  event-sourced      NO self-correction  counts dead zones

This module is the second one, and the trade in that table is the whole design.

DISCHARGE POLICY — strict, chosen deliberately. A person leaves the roster only
when a crossing into an EXIT zone is observed. There is no absence timeout and
no scheduled reset. Two consequences the caller owns:

  1. The roster MUST be persisted. It is the authoritative occupancy figure, and
     an in-memory-only roster silently resets to zero on restart while the
     building is still full. Zone occupancy can be rebuilt from the next frame;
     this cannot be rebuilt from anything.
  2. Drift is one-directional and unbounded. Every missed exit is a permanent
     phantom occupant, and nothing here corrects it. ``stale()`` exists so an
     operator can SEE the drift accumulating; it removes nobody. Auto-discharging
     the long-unseen would silently convert "we cannot see them" into "they have
     left", which is the failure the strict policy exists to avoid.

DELIBERATELY IDENTITY-AGNOSTIC. The count changes only at the doors, so it never
asks "is the person in the canteen the same one who came in at 09:04". That
question needs cross-camera re-identification; this number does not, and keeping
the dependency out is what makes the count robust while ReID is still unproven.
``ref`` is any token that stays stable for one person's crossing of a door.

Pure stdlib — unit-testable without cv2, torch or a database.
"""

from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional

# Zone types that move a person across the facility boundary.
ENTRANCE = "ENTRANCE"      # one-way in
EXIT = "EXIT"              # one-way out
DOOR = "DOOR"              # BIDIRECTIONAL — the same doorway walked both ways

# Where a zone sits relative to the facility, once doors are accounted for.
INSIDE = "INSIDE"
OUTSIDE = "OUTSIDE"
NOWHERE = "NOWHERE"        # no zone at all: uncovered floor, or off-frame

# Actions apply_event() can take, returned so a caller can log or count them.
ADMIT = "admit"
DISCHARGE = "discharge"
SEEN = "seen"
AT_DOOR = "at_door"          # crossing started, direction not yet known
TURNED_BACK = "turned_back"  # reached the door and went back the way they came
AMBIGUOUS = "ambiguous"      # crossed, but neither side was observed


class DoorPolicy:
    """Which zones are doors, and which side of the boundary everything is on.

    A one-way door is decidable the moment someone arrives in it: an ENTRANCE
    zone means in, an EXIT zone means out. A BIDIRECTIONAL door is not — the
    same polygon is walked in both directions, so arriving in it says nothing
    about which way the person is going.

    Direction there comes from the pair of zones either side of the crossing:
    where they were before the door, and where they went after it. That needs
    no coordinates, no adjacency graph and no per-door vector — only the zone
    assignment already being computed every frame.

    THE COVERAGE REQUIREMENT this creates, which is a siting decision, not a
    code one: the INSIDE of a bidirectional door must be covered by an adjacent
    zone. If the floor just inside the door belongs to no zone, then walking in
    and walking out both read as "door -> nowhere" and the crossing is
    undecidable. Those are counted as ``ambiguous_crossings`` rather than
    guessed, so inadequate door coverage shows up as a number instead of as a
    slow drift in occupancy.

    Ground drawn BEYOND the boundary — a forecourt, a car park — is declared by
    typing that zone OUTSIDE. Without it such a zone looks like interior floor
    and a person stepping out of a door onto it reads as walking in. The
    ``outside`` argument is an override for callers that cannot retype a zone.
    """

    def __init__(self, zone_types=None, outside=()):
        self.zone_types = {str(k): str(v).upper()
                           for k, v in (zone_types or {}).items()}
        self.outside = {str(z) for z in (outside or ())}

    @classmethod
    def from_zones(cls, zones) -> "DoorPolicy":
        """Build from stored zone records — the only source of truth at runtime.

        Disabled zones are dropped: a zone switched off in the editor must stop
        acting as a door, not keep counting people through a boundary the
        operator has retired.
        """
        types = {}
        for z in zones or []:
            zid = z.get("zone_id") if isinstance(z, dict) else getattr(z, "zone_id", None)
            if not zid:
                continue
            enabled = (z.get("enabled", True) if isinstance(z, dict)
                       else getattr(z, "enabled", True))
            if enabled is False or enabled == 0:
                continue
            ztype = (z.get("zone_type") if isinstance(z, dict)
                     else getattr(z, "zone_type", None))
            types[str(zid)] = str(ztype or "MONITORED").upper()
        return cls(types)

    def kind(self, zone_id) -> str:
        """Classify a zone id, including the absence of one."""
        if not zone_id:
            return NOWHERE
        zone_id = str(zone_id)
        if zone_id in self.outside:
            return OUTSIDE
        t = self.zone_types.get(zone_id)
        if t == OUTSIDE:
            return OUTSIDE
        if t in (ENTRANCE, EXIT, DOOR):
            return t
        # A zone we hold no type for is still a drawn polygon on monitored
        # floor, so it counts as interior. Guessing "door" for an unknown zone
        # would invent admissions; guessing "outside" would invent departures.
        return INSIDE

    def is_door(self, zone_id) -> bool:
        return self.kind(zone_id) in (ENTRANCE, EXIT, DOOR)

    def is_interior(self, zone_id) -> bool:
        return self.kind(zone_id) == INSIDE

    def is_beyond(self, zone_id) -> bool:
        """True if this is out of the facility, or somewhere we cannot see."""
        return self.kind(zone_id) in (OUTSIDE, NOWHERE)


class DoorCounters:
    """Per-door entry/exit totals and rates.

    Separate from the roster because they answer a different question. The
    roster is a SET — how many people are inside right now, which cannot exceed
    reality and cannot go negative. These are TALLIES — how much traffic each
    doorway has carried, which only ever grows. A door with 12,450 entries and
    9,280 exits tells you about that doorway's load; it says nothing on its own
    about how many people are in the building.

    Totals are cumulative and must be persisted with the roster. Rates are a
    rolling window and are deliberately NOT persisted: a per-minute figure
    rebuilt from an hours-old window would be fiction, and starting at zero
    after a restart is honest.
    """

    def __init__(self, window_s: float = 60.0, max_window_s: float = 900.0):
        self.window_s = window_s
        self.max_window_s = max(window_s, max_window_s)
        self._totals: Dict[str, Dict[str, int]] = {}
        self._events: Dict[str, Deque] = {}

    def record(self, door_zone: Optional[str], action: str, now: float) -> None:
        if not door_zone or action not in (ADMIT, DISCHARGE):
            return
        door = str(door_zone)
        kind = "entries" if action == ADMIT else "exits"
        t = self._totals.setdefault(door, {"entries": 0, "exits": 0})
        t[kind] += 1
        dq = self._events.setdefault(door, deque())
        dq.append((now, kind))
        self._evict(dq, now)

    def _evict(self, dq: Deque, now: float) -> None:
        cutoff = now - self.max_window_s
        while dq and dq[0][0] < cutoff:
            dq.popleft()

    def _rate(self, door: str, kind: str, now: float, window: float) -> float:
        dq = self._events.get(door)
        if not dq:
            return 0.0
        self._evict(dq, now)
        cutoff = now - window
        n = sum(1 for t, k in dq if k == kind and t >= cutoff)
        return round(n * 60.0 / window, 2) if window > 0 else 0.0

    def totals(self, door: str) -> Dict[str, int]:
        return dict(self._totals.get(str(door), {"entries": 0, "exits": 0}))

    def doors(self) -> List[str]:
        return sorted(self._totals)

    def snapshot(self, now: float, window: Optional[float] = None) -> List[dict]:
        w = window or self.window_s
        out = []
        for door in self.doors():
            t = self._totals[door]
            ein = self._rate(door, "entries", now, w)
            eout = self._rate(door, "exits", now, w)
            out.append({
                "door_zone_id": door,
                "entries": t["entries"],
                "exits": t["exits"],
                # Cumulative net, i.e. how many people this door has put inside
                # over its lifetime. Not the building's occupancy — other doors
                # exist, and the roster is the authority on that.
                "net": t["entries"] - t["exits"],
                "entry_rate_per_min": ein,
                "exit_rate_per_min": eout,
                "net_flow_per_min": round(ein - eout, 2),
                "window_s": w,
            })
        return out

    def to_records(self) -> List[dict]:
        return [{"door_zone_id": d, "entries": v["entries"], "exits": v["exits"]}
                for d, v in sorted(self._totals.items())]

    def load_records(self, records) -> None:
        for r in records or []:
            door = r.get("door_zone_id")
            if not door:
                continue
            self._totals[str(door)] = {"entries": int(r.get("entries", 0)),
                                       "exits": int(r.get("exits", 0))}


@dataclass
class Presence:
    """One person on the roster. Carries no appearance data and no PII."""

    ref: str
    admitted_at: float
    last_seen: float
    entry_zone: Optional[str] = None
    last_zone: Optional[str] = None
    sightings: int = 0

    def to_dict(self, now: Optional[float] = None) -> dict:
        d = {
            "ref": self.ref,
            "admitted_at": self.admitted_at,
            "last_seen": self.last_seen,
            "entry_zone": self.entry_zone,
            "last_zone": self.last_zone,
            "sightings": self.sightings,
        }
        if now is not None:
            # How long since anyone saw them. The number an operator needs to
            # judge whether a roster entry is a real person in a dead zone or a
            # missed exit — the roster itself cannot tell the difference.
            d["unseen_for"] = round(now - self.last_seen, 1)
            d["inside_for"] = round(now - self.admitted_at, 1)
        return d


class FacilityRoster:
    """The set of people currently inside the facility.

    Occupancy is ``len(roster)`` and is independent of how many of them any
    camera can see right now. That is the point.
    """

    def __init__(self, site_id: Optional[str] = None):
        self.site_id = site_id
        self._people: Dict[str, Presence] = {}
        self.stats = {
            "admitted": 0,          # admissions that changed the roster
            "discharged": 0,        # discharges that changed the roster
            # An admit for someone already inside. Normal and harmless (a track
            # re-crossing the entrance polygon); counted so a runaway figure is
            # visible rather than inferred.
            "readmit_ignored": 0,
            # A discharge for someone never admitted. Expected in bulk right
            # after a cold start — everyone already in the building will leave
            # without this roster having seen them arrive — and a standing
            # non-zero rate afterwards means entrance detection is missing
            # people that exit detection catches.
            "discharge_unknown": 0,
            # Bidirectional-door crossings where neither side of the door was
            # observed, so the direction could not be established. NOT guessed
            # — see DoorPolicy. A rising figure here means a door zone needs an
            # interior zone drawn against it.
            "ambiguous_crossings": 0,
            # Reached a bidirectional door and went back the way they came.
            "turned_back": 0,
        }
        # Per-door traffic tallies (REQ-14). Fed by apply_event so a door's
        # counters can never disagree with the roster movements that caused them.
        self.doors = DoorCounters()
        # ref -> (door_zone_id, zone they came from). A crossing in progress.
        # Deliberately NOT persisted: it lasts a few seconds, and a restart
        # mid-crossing should drop the crossing rather than resolve it on stale
        # information.
        self._crossing: Dict[str, tuple] = {}

    # ---- the three transitions -------------------------------------------
    def admit(self, ref: str, now: float, zone_id: Optional[str] = None) -> bool:
        """Person crossed in. Returns True if the roster actually grew."""
        if not ref:
            return False
        existing = self._people.get(ref)
        if existing is not None:
            # Already inside. Refresh the sighting but do NOT count them twice —
            # someone standing in the entrance polygon re-triggers this.
            existing.last_seen = now
            if zone_id:
                existing.last_zone = zone_id
            existing.sightings += 1
            self.stats["readmit_ignored"] += 1
            return False
        self._people[ref] = Presence(ref=ref, admitted_at=now, last_seen=now,
                                     entry_zone=zone_id, last_zone=zone_id,
                                     sightings=1)
        self.stats["admitted"] += 1
        return True

    def discharge(self, ref: str, now: float,
                  zone_id: Optional[str] = None) -> bool:
        """Person crossed out. Returns True if the roster actually shrank."""
        if not ref:
            return False
        if ref not in self._people:
            # Never let occupancy go negative by "removing" someone who was
            # never counted. The discrepancy is recorded, not absorbed.
            self.stats["discharge_unknown"] += 1
            return False
        del self._people[ref]
        self.stats["discharged"] += 1
        return True

    def note_seen(self, ref: str, now: float,
                  zone_id: Optional[str] = None) -> bool:
        """A sighting inside the facility. Never admits.

        Returns False for anyone not on the roster — including someone already
        discharged, whose trailing events would otherwise resurrect them and
        make the exit un-count itself.
        """
        p = self._people.get(ref)
        if p is None:
            return False
        p.last_seen = now
        if zone_id:
            p.last_zone = zone_id
        p.sightings += 1
        return True

    # ---- bidirectional crossings -----------------------------------------
    # A crossing of a two-way door is only half-observed when the person
    # arrives in it. These hold the half until the other side is seen.
    def begin_crossing(self, ref: str, door_zone: str,
                       from_zone: Optional[str]) -> None:
        self._crossing[ref] = (door_zone, from_zone)

    def pop_crossing(self, ref: str) -> Optional[tuple]:
        return self._crossing.pop(ref, None)

    def crossing_zone(self, ref: str) -> Optional[str]:
        c = self._crossing.get(ref)
        return c[0] if c else None

    def pending_crossings(self) -> int:
        return len(self._crossing)

    # ---- queries ----------------------------------------------------------
    def occupancy(self) -> int:
        """People inside, seen or not. THE number this module exists to produce."""
        return len(self._people)

    def contains(self, ref: str) -> bool:
        return ref in self._people

    def get(self, ref: str) -> Optional[Presence]:
        return self._people.get(ref)

    def members(self, now: Optional[float] = None) -> List[dict]:
        return [p.to_dict(now) for p in
                sorted(self._people.values(), key=lambda p: p.admitted_at)]

    def stale(self, older_than_s: float, now: float) -> List[dict]:
        """Roster entries nobody has seen for a while. READ-ONLY — removes none.

        This is the drift report. Under the strict policy an entry here is
        either a person genuinely in an unmonitored space or an exit that was
        missed, and no amount of data in this module distinguishes them. A human
        decides; that is why this returns a list instead of deleting rows.
        """
        return [p.to_dict(now) for p in
                sorted(self._people.values(), key=lambda p: p.last_seen)
                if (now - p.last_seen) > older_than_s]

    def snapshot(self, now: Optional[float] = None,
                 stale_after_s: float = 3600.0) -> dict:
        body = {
            "site_id": self.site_id,
            "occupancy": self.occupancy(),
            "pending_crossings": self.pending_crossings(),
            "stats": dict(self.stats),
        }
        if now is not None:
            body["stale"] = len(self.stale(stale_after_s, now))
            body["stale_after_s"] = stale_after_s
            # Rates need a clock, so per-door figures only appear when one is
            # supplied. Totals alone would be misleading without them.
            body["doors"] = self.doors.snapshot(now)
            body["ts"] = now
        return body

    # ---- persistence ------------------------------------------------------
    # The roster is authoritative and cannot be recomputed from live frames, so
    # it has to round-trip through the store. Plain dicts, no embeddings.
    def to_records(self) -> List[dict]:
        return [p.to_dict() for p in self._people.values()]

    @classmethod
    def from_records(cls, records, site_id: Optional[str] = None,
                     stats: Optional[dict] = None,
                     doors: Optional[List[dict]] = None) -> "FacilityRoster":
        roster = cls(site_id=site_id)
        roster.doors.load_records(doors)
        for r in records or []:
            ref = r.get("ref")
            if not ref:
                continue
            roster._people[ref] = Presence(
                ref=ref,
                admitted_at=float(r.get("admitted_at", 0.0)),
                last_seen=float(r.get("last_seen", r.get("admitted_at", 0.0))),
                entry_zone=r.get("entry_zone"),
                last_zone=r.get("last_zone"),
                sightings=int(r.get("sightings", 0)),
            )
        # Counters are restored rather than reset: they are a running record of
        # how the roster has behaved, and zeroing them on restart would hide
        # exactly the drift they exist to expose.
        for k, v in (stats or {}).items():
            if k in roster.stats:
                roster.stats[k] = int(v)
        return roster


# ---- event -> roster mapping ---------------------------------------------
# Which zone a person has ARRIVED in, per event type. Arrival is what moves the
# roster; a ZONE_EXIT says only that they left somewhere, not where they went.
def _arrival_zone(event: dict) -> Optional[str]:
    et = event.get("event_type")
    if et in ("ZONE_ENTRY", "ZONE_TRANSITION"):
        return event.get("zone_to")
    return None


def _resolve_crossing(roster: FacilityRoster, policy: DoorPolicy, ref: str,
                      now: float, from_zone, door_zone, to_zone) -> str:
    """Decide which way a two-way door was walked, from the zones either side.

    The four cases, and why each falls the way it does:

      inside  -> door -> beyond   they left the building          DISCHARGE
      beyond  -> door -> inside   they came in                    ADMIT
      inside  -> door -> inside   reached the door, turned back    no change
      beyond  -> door -> beyond   both sides unobserved            no change

    The last case is the honest one. It is what happens when the floor inside
    the door belongs to no zone: entering and leaving produce the identical
    event sequence, and nothing in the data separates them. Picking a direction
    there would be a coin toss applied to the authoritative occupancy figure,
    so it is counted and left alone.
    """
    came_from_inside = policy.is_interior(from_zone)
    went_beyond = policy.is_beyond(to_zone)

    # Door tallies record the CROSSING, whether or not the roster moved. A
    # cold-start exit by someone never admitted is still traffic through that
    # door, and the gap between a door's totals and the roster's admitted /
    # discharged counters is exactly the drift signal worth watching.
    if came_from_inside and went_beyond:
        roster.discharge(ref, now, door_zone)
        roster.doors.record(door_zone, DISCHARGE, now)
        return DISCHARGE
    if not came_from_inside and not went_beyond:
        roster.admit(ref, now, to_zone)
        roster.doors.record(door_zone, ADMIT, now)
        return ADMIT
    if came_from_inside and not went_beyond:
        roster.note_seen(ref, now, to_zone)
        roster.stats["turned_back"] += 1
        return TURNED_BACK
    roster.stats["ambiguous_crossings"] += 1
    return AMBIGUOUS


def apply_event(roster: FacilityRoster, event: dict, zone_types) -> Optional[str]:
    """Feed one zone event to the roster. Returns the action taken, or None.

    ``zone_types`` is a DoorPolicy, or a plain zone_id -> zone_type mapping
    which is wrapped in the default policy.

    One-way doors resolve on ARRIVAL, because arriving is already the whole
    story:

      arrive in an ENTRANCE zone -> admit
      arrive in an EXIT zone     -> discharge

    A two-way DOOR zone resolves on DEPARTURE instead. Arriving in it only
    opens a crossing; the direction is settled when the person turns up
    somewhere else, or leaves zone coverage entirely. Everything else is a
    sighting, which never changes the count.
    """
    if not isinstance(event, dict):
        return None
    policy = (zone_types if isinstance(zone_types, DoorPolicy)
              else DoorPolicy(zone_types))
    ref = event.get("person_ref")
    ts = event.get("ts")
    if not ref or not isinstance(ts, (int, float)) or isinstance(ts, bool):
        return None
    now = float(ts)

    arrival = _arrival_zone(event)
    pending = roster.pop_crossing(ref)

    if arrival:
        kind = policy.kind(arrival)
        if kind == DOOR:
            # Walking from one door straight into another tells us nothing new;
            # the crossing simply moves to the newer door. Keep the ORIGINAL
            # origin zone, which is the half that carries the direction.
            origin = pending[1] if pending else event.get("zone_from")
            roster.begin_crossing(ref, arrival, origin)
            return AT_DOOR
        if pending:
            # They have come out of a two-way door into somewhere we can see.
            return _resolve_crossing(roster, policy, ref, now,
                                     from_zone=pending[1], door_zone=pending[0],
                                     to_zone=arrival)
        if kind == ENTRANCE:
            roster.admit(ref, now, arrival)
            roster.doors.record(arrival, ADMIT, now)
            return ADMIT
        if kind == EXIT:
            roster.discharge(ref, now, arrival)
            roster.doors.record(arrival, DISCHARGE, now)
            return DISCHARGE
        roster.note_seen(ref, now, arrival)
        return SEEN

    # No arrival zone: a departure, or an in-place event (loitering, restricted).
    where = (event.get("zone_id") or event.get("zone_from"))
    if pending and event.get("event_type") == "ZONE_EXIT" \
            and str(where) == str(pending[0]):
        # Left the two-way door and landed in no zone at all — off frame, or
        # onto floor nobody covers. That is "beyond" as far as this can tell.
        return _resolve_crossing(roster, policy, ref, now,
                                 from_zone=pending[1], door_zone=pending[0],
                                 to_zone=None)
    if pending:
        # Some other event while mid-crossing (loitering in the doorway). The
        # crossing is still open, so put it back rather than dropping it.
        roster.begin_crossing(ref, pending[0], pending[1])
    if roster.note_seen(ref, now, where):
        return SEEN
    return None
