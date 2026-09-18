"""Framework-agnostic ingest/query service.

All the API's business logic lives here so it is unit-testable without FastAPI,
Redis, or Postgres. app.py is a thin HTTP adapter over this class.
"""

import logging
import os
import time
from typing import Dict, List, Optional, Tuple

from finblade import links as _links
from finblade import org as _org
from finblade import gps as _trk
from finblade.emission import DEFAULT_KEEPALIVE, StateWriteGate
from finblade.events import (FACILITY_ENTRY, FACILITY_EXIT, PERSON_ATTRIBUTES,
                             TRACKER_ARRIVED, TRACKER_DEPARTED, new_event)
from finblade.presence import (
    ADMIT, DISCHARGE, DoorPolicy, FacilityRoster, apply_event,
)

from .bus import FACILITY_STREAM
from .schema import validate_ingest, validate_zone_state, validate_zones
from .store import Store
from .webhooks import WebhookDispatcher

# Shape marker on every fb:facility record. A consumer branches on this rather
# than sniffing for fields, and it is bumped on a breaking change — the same
# habit as the forwarder's envelope version.
FACILITY_COUNTS = "FACILITY_COUNTS"
FACILITY_SCHEMA_VERSION = 1

log = logging.getLogger("finblade.service")

# How long a door policy built from the zones table is reused before rebuilding.
# Zones change when an operator saves the editor, which also invalidates this
# explicitly — the interval only bounds staleness if a zone is written by some
# other path.
_POLICY_TTL_S = 30.0

# A sighting only moves a roster entry's last_seen, which nothing depends on
# second-by-second, so those writes are batched. A crossing is written through
# immediately and never batched.
_PRESENCE_FLUSH_S = 30.0

# Upper bound on a declared opening headcount. Not a capacity limit — it is a
# typo guard, so a fat-fingered 40000 cannot bury the observed roster under a
# number no building holds.
_MAX_BASELINE = 100_000


def _crop_url(row: dict) -> Optional[str]:
    """Signed link to a sighting's crop, or None when it has no crop."""
    if not row.get("frame") or not row.get("event_id"):
        return None
    return _links.sign(f"/api/v1/search/sightings/{row['event_id']}/crop")[0]


class IngestService:
    def __init__(self, store: Store, bus=None, state_gate=None, counts_gate=None):
        self.store = store
        self.bus = bus  # optional event bus with .publish(evt); None = skip
        # The facility roster is the one piece of state here that cannot be
        # recomputed from live video, so it is restored from the store at
        # startup rather than starting empty. See finblade/presence.py.
        try:
            people, stats, doors = self.store.load_presence()
        except Exception:                                   # noqa: BLE001
            people, stats, doors = [], {}, []
        # D-42: an exit whose ref matches nobody still discharges someone
        # (the longest-present) — evict_oldest — unless a site asks for the
        # original strict behaviour with FINBLADE_PRESENCE_UNMATCHED_EXIT=ignore.
        # FINBLADE_PRESENCE_EXPIRE_HOURS (0 = off) retires anyone "inside"
        # longer than a plausible visit; see FacilityRoster.expire.
        policy = (os.environ.get("FINBLADE_PRESENCE_UNMATCHED_EXIT") or "evict_oldest").strip().lower()
        if policy not in FacilityRoster.UNMATCHED_EXIT_POLICIES:
            log.warning("FINBLADE_PRESENCE_UNMATCHED_EXIT=%r is not one of %s; using evict_oldest",
                        policy, FacilityRoster.UNMATCHED_EXIT_POLICIES)
            policy = "evict_oldest"
        try:
            self.presence_expire_s = max(0.0, float(os.environ.get("FINBLADE_PRESENCE_EXPIRE_HOURS") or 0)) * 3600.0
        except ValueError:
            self.presence_expire_s = 0.0
        self.roster = FacilityRoster.from_records(people, stats=stats, doors=doors,
                                                  unmatched_exit=policy)
        self._policy: Optional[DoorPolicy] = None
        self._policy_at = 0.0
        self._presence_dirty = False
        self._presence_flushed_at = 0.0
        # Write-on-change for zone_state_ts. See finblade/emission.py; set
        # FINBLADE_STATE_WRITES=always to restore a row per post.
        self.state_gate = state_gate if state_gate is not None else StateWriteGate(
            os.environ.get("FINBLADE_STATE_WRITES", "change"),
            float(os.environ.get("FINBLADE_STATE_KEEPALIVE", DEFAULT_KEEPALIVE) or 0))
        # Same gate, separate knobs, for the fb:facility counts stream. Separate
        # because the two answer different questions: the zone gate decides what
        # enters a history table that has to stay small over months, this one
        # decides what a live consumer is told. Sharing one variable would mean
        # setting `always` to debug a zone-history problem also floods the
        # counts stream, which is how one knob becomes two bugs.
        self.counts_gate = counts_gate if counts_gate is not None else StateWriteGate(
            os.environ.get("FINBLADE_COUNT_WRITES", "change"),
            float(os.environ.get("FINBLADE_COUNT_KEEPALIVE", DEFAULT_KEEPALIVE) or 0))
        self.counts_published = 0
        self.counts_errors = 0
        # GPS geofences. Fences are rebuilt from the branches table when it
        # changes; the per-tracker state is restored from tracker_live so a
        # vehicle already parked at a branch does not re-arrive on restart.
        self._geo = _trk.GeofenceEngine()
        self._geo_loaded = 0.0
        self._geo_restored = False
        self._tracker_silent = {}      # tracker_id -> True while R-12 is open
        # Outbound webhooks. notify() only writes a queue row, so it sits on
        # the alert path safely; app.py runs the delivery loop.
        self.webhooks = WebhookDispatcher(
            lambda: self.store, scope_resolver=self.branches_in_scope,
            context_provider=self._webhook_context,
            tenant_provider=lambda: {k.replace("tenant_", ""): v
                                     for k, v in self.store.get_org_meta().items()
                                     if k.startswith("tenant_")})

    # -- POST /api/v1/events/ingest --
    def ingest_event(self, payload: dict) -> Tuple[int, dict]:
        ok, errors = validate_ingest(payload)
        if not ok:
            return 422, {"accepted": False, "errors": errors}
        self.store.save_event(payload)
        # Any event from a camera counts as a heartbeat for offline detection.
        # Tracker events carry the tracker id in camera_id (there is no
        # camera) and must NOT mint a camera row, or every vehicle would show
        # up on the Cameras page as a camera that is permanently offline.
        if payload.get("event_type") not in (TRACKER_ARRIVED, TRACKER_DEPARTED):
            self.store.mark_camera_seen(payload.get("camera_id"), payload.get("timestamp"),
                                        payload.get("site_id"))
        if payload.get("event_type") == PERSON_ATTRIBUTES:
            self._save_sighting(payload)
        action = self._apply_presence(payload)
        if self.bus is not None:
            self.bus.publish(payload)
        body = {"accepted": True, "event_id": payload.get("event_id")}
        if action:
            # Echoed so a camera worker (or a test) can see that its event moved
            # the facility count, without a second round trip.
            body["facility"] = {"action": action,
                                "occupancy": self.roster.occupancy()}
        return 202, body

    # -- facility roster -----------------------------------------------------
    def door_policy(self, now: float = None) -> DoorPolicy:
        """Door policy derived from the zones table, rebuilt periodically."""
        now = time.time() if now is None else now
        if self._policy is None or (now - self._policy_at) > _POLICY_TTL_S:
            try:
                cams = {c.get("camera_id") for c in self.store.list_cameras()
                        if c.get("camera_id")}
                self._policy = DoorPolicy.from_zones(self.store.list_zones(),
                                                     cameras=cams)
            except Exception:                               # noqa: BLE001
                # A store hiccup must not silently turn every door into
                # interior floor, which would stop all counting. Keep the last
                # good policy if there is one.
                self._policy = self._policy or DoorPolicy({})
            self._policy_at = now
        return self._policy

    def invalidate_door_policy(self) -> None:
        """Called when zones change so a retyped door takes effect at once."""
        self._policy = None

    def _apply_presence(self, evt: dict) -> Optional[str]:
        """Feed one event to the facility roster and persist what changed."""
        ts = evt.get("timestamp")
        # apply_event expects `ts`; the wire envelope calls it `timestamp`.
        view = dict(evt)
        view["ts"] = ts
        # apply_event keys on person_ref; substitute the resolved key so the
        # roster is identity-keyed without presence.py needing to know which
        # kind of ref it was handed.
        _resolved_key = None
        # WHICH KEY THE ROSTER COUNTS BY — the difference between an occupancy
        # figure and a churn counter.
        #
        # person_ref is a hash of the tracker id: it changes every time tracking
        # breaks, so keying on it admits the same human once per fragment. Live,
        # that produced 616 admissions and an occupancy of 600 from a camera
        # showing 21 people. global_ref is the cross-camera identity and survives
        # a track break, which is the property this count actually needs.
        #
        # The fallback is deliberate but is NOT free: when ReID has not resolved
        # a track there is no stable key available, so the entry is counted as
        # provisional and reported separately rather than being quietly mixed in
        # with the trustworthy ones.
        gref = evt.get("global_ref")
        pref = evt.get("person_ref")
        ref = gref or pref
        if not gref and pref:
            view["_provisional"] = True
        elif gref and pref:
            # ReID has resolved this track, and it may well have been admitted
            # before it did — the views of somebody in a doorway are the worst
            # crops the gate ever sees, so resolution usually lands AFTER the
            # entry crossing, not before it. In that case the roster entry is
            # keyed on the tracker hash while every event from here on carries
            # the global ref, and their exit would remove nothing.
            #
            # Cheap to attempt and a no-op unless a stale entry is actually
            # sitting there, so it runs on every resolved event rather than
            # needing to detect the transition.
            self.roster.rekey(pref, gref)
        # Which doorway this is, captured BEFORE the crossing resolves: for a
        # two-way door the pending crossing knows it, and resolving pops it.
        door = self.roster.crossing_zone(ref) if ref else None
        if ref:
            view["person_ref"] = ref
            _resolved_key = ref
        try:
            action = apply_event(self.roster, view, self.door_policy(ts))
        except Exception:                                   # noqa: BLE001
            # Presence is additive to ingest. A bug here must never reject a
            # camera's event or stop the pipeline.
            return None
        if action == ADMIT and view.get("_provisional"):
            # Admitted without a stable identity. Counted so the share of the
            # roster that cannot be trusted is a number an operator can read,
            # rather than something they discover when occupancy disagrees with
            # the room by an order of magnitude.
            self.roster.stats["provisional_admits"] = (
                self.roster.stats.get("provisional_admits", 0) + 1)
        if action in (ADMIT, DISCHARGE):
            self._emit_facility_event(evt, action,
                                      door or view.get("zone_to")
                                      or view.get("zone_from"))
            self._flush_presence(force=True)
            # A crossing is the only thing that moves the headline count, so the
            # counts stream reacts here rather than waiting for the next
            # background tick. The gate still decides — it will always say yes
            # on a crossing, because occupancy is its change key — so the
            # periodic loop remains purely the keepalive.
            self.publish_facility_counts(evt.get("timestamp"))
        elif action:
            self._presence_dirty = True
            self._flush_presence()
        return action

    def _emit_facility_event(self, source: dict, action: str,
                             door_zone_id) -> None:
        """Record the building crossing itself, not just the zone move.

        Without this a facility entry is only inferable by replaying every zone
        event through the door policy — so "18:02:14 entered facility" could not
        appear in a person's history, which is exactly what the movement history
        is asked for. The resulting occupancy rides along so the figure can be
        reconstructed from the event stream alone.
        """
        etype = FACILITY_ENTRY if action == ADMIT else FACILITY_EXIT
        evt = new_event(etype, source.get("camera_id") or "",
                        source.get("site_id") or "",
                        source.get("timestamp") or time.time(),
                        door_zone_id=str(door_zone_id or ""),
                        person_ref=source.get("person_ref"),
                        occupancy=self.roster.occupancy())
        if source.get("track_id") is not None:
            evt["track_id"] = source["track_id"]
        try:
            self.store.save_event(evt)
            if self.bus is not None:
                self.bus.publish(evt)
        except Exception:                                   # noqa: BLE001
            return

    def _flush_presence(self, force: bool = False, now: float = None) -> None:
        now = time.time() if now is None else now
        if not force and not self._presence_dirty:
            return
        if not force and (now - self._presence_flushed_at) < _PRESENCE_FLUSH_S:
            return
        try:
            # baseline travels in the stats dict — see FacilityRoster
            # .from_records for why it is not a fourth store argument.
            self.store.save_presence(self.roster.to_records(),
                                     dict(self.roster.stats,
                                          baseline=self.roster.baseline),
                                     self.roster.doors.to_records())
        except Exception:                                   # noqa: BLE001
            return
        self._presence_dirty = False
        self._presence_flushed_at = now

    def facility_counts_record(self, now: float = None,
                               stale_after_s: float = 3600.0) -> dict:
        """The merged-count record published to fb:facility.

        Counted on GLOBAL IDENTITY door crossings, never on per-camera detection
        counts — see finblade/presence.py. That is the whole reason this stream
        exists separately from summing zone occupancy: a person standing where
        two cameras overlap is one person here, and a person in a corridor no
        camera watches is still counted.
        """
        now = time.time() if now is None else now
        body = self.roster.snapshot(now=now, stale_after_s=stale_after_s)
        body["record_type"] = FACILITY_COUNTS
        body["schema_version"] = FACILITY_SCHEMA_VERSION
        return body

    def publish_facility_counts(self, now: float = None,
                                force: bool = False) -> Optional[dict]:
        """Publish merged counts to fb:facility if the gate allows it.

        Returns the published record, or None if it was suppressed as unchanged
        or there is no bus. Never raises: a bus problem must not be able to
        reject an event or break a crossing, which is why the caller in
        _apply_presence can ignore the result.
        """
        if self.bus is None:
            return None
        now = time.time() if now is None else now
        occupancy = self.roster.occupancy()
        if not force:
            # Occupancy alone is the change key, deliberately. baseline and
            # observed are its two components and cannot move without moving it;
            # door tallies only change on a crossing, which changes it too. What
            # this excludes is `stale`, which creeps upward with the clock alone
            # and would defeat the gate entirely, republishing every tick for a
            # number a consumer can recompute.
            if not self.counts_gate.should_write(
                    self.roster.site_id or "", "__facility__",
                    occupancy, None, now):
                return None
        record = self.facility_counts_record(now=now)
        try:
            self.bus.publish_to(FACILITY_STREAM, record)
        except Exception:                                   # noqa: BLE001
            # Counted rather than swallowed silently: a stream that has been
            # failing all day must be visible in /api/v1/health, not inferred
            # from a dashboard that stopped moving.
            self.counts_errors += 1
            return None
        self.counts_published += 1
        return record

    def counts_stats(self) -> dict:
        return {"stream": FACILITY_STREAM,
                "published": self.counts_published,
                "errors": self.counts_errors,
                "bus": type(self.bus).__name__ if self.bus else None,
                "gate": self.counts_gate.stats()}

    def expire_presence(self, now: float = None) -> int:
        """Retire roster entries older than FINBLADE_PRESENCE_EXPIRE_HOURS.
        A no-op when unset. Called from the facility tick and before a
        snapshot, so the horizon holds without a dedicated timer."""
        if not self.presence_expire_s:
            return 0
        now = time.time() if now is None else now
        gone = self.roster.expire(self.presence_expire_s, now)
        if gone:
            log.info("facility: expired %d roster entr%s older than %.1f h",
                     len(gone), "y" if len(gone) == 1 else "ies", self.presence_expire_s / 3600.0)
            self._flush_presence(force=True, now=now)
            self.publish_facility_counts(now)
        return len(gone)

    def facility_state(self, now: float = None, stale_after_s: float = 3600.0) -> dict:
        now = time.time() if now is None else now
        self.expire_presence(now)
        body = self.roster.snapshot(now=now, stale_after_s=stale_after_s)
        body["unmatched_exit"] = self.roster.unmatched_exit
        body["expire_after_s"] = self.presence_expire_s or None
        body["policy"] = {
            "doors": sorted(z for z, t in self.door_policy(now).zone_types.items()
                            if t in ("DOOR", "ENTRANCE", "EXIT")),
            "outside": sorted(z for z, t in self.door_policy(now).zone_types.items()
                              if t == "OUTSIDE"),
        }
        return body

    def clear_facility(self) -> Tuple[int, dict]:
        """Operator reset for a drifted roster. Persisted immediately."""
        removed = self.roster.clear()
        self._flush_presence(force=True)
        return 200, {"ok": True, "removed": removed,
                     "occupancy": self.roster.occupancy(),
                     "note": "Door tallies and lifetime counters are kept — they "
                             "record observed traffic, which stays true. Any "
                             "declared baseline is cleared: it claims people are "
                             "inside, which resetting to empty contradicts."}

    def set_facility_baseline(self, payload: dict) -> Tuple[int, dict]:
        """Declare how many people were already inside at startup.

        The count cannot be measured from here. An interior sighting proves one
        person is inside but says nothing about the rooms no camera watches, so
        a number derived from cameras would be an unknowable fraction of the
        truth presented as the whole of it. This takes the figure from wherever
        the site actually knows it — badge system, fire register, a walk round —
        and then lets the exits drain it.
        """
        if not isinstance(payload, dict):
            return 422, {"ok": False, "errors": ["payload must be an object"]}
        raw = payload.get("count")
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return 422, {"ok": False, "errors": ["count must be a number"]}
        if isinstance(raw, float) and raw != int(raw):
            # A headcount is people. Silently truncating 12.7 would be a
            # measurement error accepted as a fact.
            return 422, {"ok": False, "errors": ["count must be a whole number"]}
        if raw < 0:
            return 422, {"ok": False, "errors": ["count must be >= 0"]}
        if raw > _MAX_BASELINE:
            return 422, {"ok": False,
                         "errors": [f"count must be <= {_MAX_BASELINE}"]}
        self.roster.set_baseline(int(raw))
        self._flush_presence(force=True)
        return 200, {"ok": True, "baseline": self.roster.baseline,
                     "observed": self.roster.observed(),
                     "occupancy": self.roster.occupancy(),
                     "note": "Drains by one each time somebody leaves who was "
                             "never seen to arrive, so it decays to zero as the "
                             "opening population turns over."}

    def facility_members(self, now: float = None) -> List[dict]:
        return self.roster.members(now=time.time() if now is None else now)

    def facility_stale(self, older_than_s: float, now: float = None) -> List[dict]:
        return self.roster.stale(older_than_s,
                                 time.time() if now is None else now)

    # -- POST /api/v1/zones/state --
    def record_zone_state(self, payload: dict) -> Tuple[int, dict]:
        ok, errors = validate_zone_state(payload)
        if not ok:
            return 422, {"accepted": False, "errors": errors}
        if not payload.get("site_id"):
            site = self.site_for_camera(payload.get("camera_id"))
            if site:
                payload = dict(payload, site_id=site)
        # The live reading is updated on EVERY post; only the history append is
        # gated. This is what keeps a quiet zone present in /zones/state — that
        # endpoint reads zone_live and drops anything older than 30 seconds, so
        # suppressing the live write too would make an unchanging zone vanish
        # from the dashboard half a minute after it settled.
        history = self.state_gate.should_write(
            payload.get("camera_id"), payload["zone_id"],
            payload.get("occupancy"), payload.get("status"), payload.get("ts"))
        self.store.save_zone_state(payload, history=history)
        # Feed the physical-area view. Only zones an operator mapped to an area
        # take this path; everything else is untouched.
        self._observe_area(payload)
        # 5s zone-state posts are the camera's primary heartbeat. Unconditional:
        # a suppressed history row is still proof the camera is alive, and
        # gating this would make every quiet zone trip R-07.
        self.store.mark_camera_seen(payload.get("camera_id"), payload.get("ts"))
        return 202, {"accepted": True, "zone_id": payload["zone_id"],
                     "recorded": history}

    # -- history / logs --
    def events_history(self, t0, t1, **f):
        return self.store.list_events(t0, t1, **f)

    def rebind_global_ref(self, drop_ref, keep_ref) -> int:
        """Apply an identity merge to stored history. Returns rows updated."""
        return self.store.rebind_global_ref(drop_ref, keep_ref)

    def alerts_history(self, t0, t1, **f):
        return self.store.list_alerts_history(t0, t1, **f)

    def cameras(self):
        return self.store.list_cameras()

    def record_camera_health(self, payload: dict) -> Tuple[int, dict]:
        """Ingest a health snapshot from an inference runner (Req 4/5).

        Returns the desired control state so the runner can drive simulate/restore
        centrally on its next heartbeat (no inbound connection to the runner needed).
        """
        cid = payload.get("camera_id")
        if not cid:
            return 422, {"ok": False, "errors": ["camera_id required"]}
        health = payload.get("health") or payload
        ts = payload.get("ts") or payload.get("timestamp") or time.time()
        self.store.record_camera_health(cid, health, ts, site_id=payload.get("site_id"))
        cam = next((c for c in self.store.list_cameras()
                    if c.get("camera_id") == cid), {})
        return 200, {"ok": True, "control": {"simulate": bool(cam.get("sim_failure"))}}

    def set_camera_sim(self, camera_id: str, on: bool) -> Tuple[int, dict]:
        self.store.set_camera_sim(camera_id, on)
        return 200, {"ok": True, "camera_id": camera_id, "simulate": bool(on)}

    def upsert_camera(self, payload: dict) -> Tuple[int, dict]:
        cid = (payload.get("camera_id") or "").strip()
        if not cid:
            return 422, {"ok": False, "errors": ["camera_id required"]}
        self.store.upsert_camera(cid, site_id=payload.get("site_id"),
                                 name=payload.get("name"),
                                 stream_url=payload.get("stream_url"),
                                 source=payload.get("source"),
                                 enabled=(None if payload.get("enabled") is None
                                          else (1 if payload.get("enabled") else 0)))
        return 200, {"ok": True, "camera_id": cid}

    def delete_camera(self, camera_id: str) -> Tuple[int, dict]:
        ok = self.store.delete_camera(camera_id)
        return (200 if ok else 404), {"ok": ok, "camera_id": camera_id}

    def movement(self, t0, t1, camera_id=None):
        """Aggregate zone->zone transitions in a window into from/to counts."""
        from collections import Counter
        evs = self.store.list_events(t0, t1, camera_id=camera_id,
                                     event_type="ZONE_TRANSITION", limit=5000)
        c = Counter((e.get("zone_from"), e.get("zone_to")) for e in evs
                    if e.get("zone_from") and e.get("zone_to"))
        return [{"zone_from": f, "zone_to": t, "count": n}
                for (f, t), n in c.most_common()]

    def occupancy_stats(self, t0, t1, **f):
        return self.store.zone_state_stats(t0, t1, **f)

    def identity_window_counts(self, t0, t1):
        """Distinct people seen between t0 and t1, from stored history."""
        return self.store.identity_window_counts(t0, t1)

    _TW_FIELDS = ("occupancy", "density", "capacity_pct")

    # Matches the sentinel the history routes already use for "no upper bound".
    # A literal float("inf") reaches SQLite as an Inf REAL and compares in ways
    # that differ from the in-memory store; a large finite number does not.
    FAR_FUTURE = 9_000_000_000_000.0

    # A day of 1-second buckets is 86,400 rows. Nobody reads that, and a model
    # choosing its own arguments will ask for it. Buckets are coarsened rather
    # than the request rejected, and the response says what it actually used —
    # a 400 costs a round trip and the model usually retries with a guess.
    #
    # 1000 because that is the chart tag's point cap (charts.MAX_POINTS). Going
    # finer here would make the JSON and the chart disagree, and the chart
    # would be trimmed to its FIRST 1000 points — for a time series that means
    # silently dropping the most recent data, which is the part anyone asking
    # "show me the last six hours" actually wants.
    MAX_BUCKETS = 1000

    def max_hold(self):
        """How long one sample may speak for, in seconds, or None.

        A killed worker emits no CAMERA_OFFLINE, so the gap it leaves is
        invisible in the event log and the sample before it would otherwise
        hold across however many hours the process was down. The keepalive is
        what makes that detectable: with a write guaranteed every N seconds, a
        gap materially longer than N means the camera was not running.

        Twice the interval, so a single late or dropped keepalive does not
        register as an outage. With the keepalive disabled there is nothing to
        measure silence against, and holding indefinitely is all that is left.
        """
        keepalive = getattr(self.state_gate, "keepalive_s", 0) or 0
        return 2 * keepalive if keepalive > 0 else None

    def camera_outages(self, camera_id, t0, t1):
        """Known offline periods for one camera, from the event log.

        Looks back a day before the window: a camera that went down on Sunday
        and is still down on Monday emits nothing inside a Monday window, and
        without the lookback the outage would be invisible exactly when it has
        lasted longest.
        """
        from finblade.timeweight import offline_intervals
        events = self.store.list_events(t0 - 86400.0, t1, camera_id=camera_id,
                                        limit=5000)
        return offline_intervals(events, t0, t1)

    def resolve_zone(self, zone_id, camera_id=None):
        """Which (camera_id, zone_id) a caller means.

        Zone ids are unique only within a camera — the editor numbers every
        camera's zones from ZONE-01 — so a bare "ZONE-01" can name several
        physically unrelated areas. Returns the list of matches and lets the
        caller refuse rather than picking one: summing the lobby and the
        loading bay because they share an id produces a number that is wrong in
        a way nobody can see downstream.
        """
        # BOTH sources, unioned — not config with history as a fallback.
        #
        # Configured-but-never-reported and reported-but-not-configured are
        # both real. A zone deleted from the config still has history, so "show
        # me last week" has to keep working; and a camera whose zones were
        # never saved through the editor still posts state.
        #
        # Checking config first and only falling back when it was empty looked
        # equivalent and was not: with ZONE-01 configured on one camera and
        # merely reporting on another, the config lookup found exactly one
        # match and the query was answered for that camera without a word.
        # Silently picking the wrong physical area is the failure this whole
        # function exists to prevent. Caught by the live check, not the unit
        # tests, because the fixtures there configured every camera.
        seen = []

        def note(row):
            key = (row.get("camera_id"), row.get("zone_id"))
            if key not in seen:
                seen.append(key)

        for row in self.store.list_zones() or []:
            if row.get("zone_id") != zone_id:
                continue
            if camera_id and row.get("camera_id") != camera_id:
                continue
            note(row)
        for row in self.store.zone_state_prior(self.FAR_FUTURE,
                                               camera_id=camera_id,
                                               zone_id=zone_id):
            note(row)
        return seen

    def zone_time_weighted(self, t0, t1, camera_id=None, zone_id=None):
        """Time-weighted stats per (camera_id, zone_id), keyed by that pair.

        The SQL AVG() in zone_state_stats weights every row equally, which is
        only correct while rows arrive on a fixed cadence. Once writes are
        sparse — step 4 — one row can stand for four seconds and the next for
        four hours, and averaging rows answers a different question.

        Camera downtime is excluded from the denominator rather than counted as
        empty, and reported as `coverage`. Under sparse writes a gap in the
        samples means "nothing changed"; a gap in camera liveness means "we do
        not know". They are indistinguishable in zone_state_ts, which is why
        the outage windows come from the event log instead.
        """
        from finblade.timeweight import offline_intervals, time_weighted

        rows = self.store.zone_state_rows(t0, t1, camera_id=camera_id,
                                          zone_id=zone_id)
        priors = {(r.get("camera_id"), r.get("zone_id")): r
                  for r in self.store.zone_state_prior(t0, camera_id=camera_id,
                                                       zone_id=zone_id)}
        by_zone = {}
        for row in rows:
            by_zone.setdefault((row.get("camera_id"), row.get("zone_id")),
                               []).append(row)
        for key in priors:
            by_zone.setdefault(key, [])

        # Outages are per camera, so fetch once per camera rather than per zone.
        gaps_by_camera = {}
        for cam, _zone in by_zone:
            if cam in gaps_by_camera:
                continue
            cam_events = self.store.list_events(t0 - 86400.0, t1, camera_id=cam,
                                                limit=5000)
            gaps_by_camera[cam] = offline_intervals(cam_events, t0, t1)

        max_hold = self.max_hold()

        out = {}
        for key, samples in by_zone.items():
            out[key] = time_weighted(samples, t0, t1, self._TW_FIELDS,
                                     unknown=gaps_by_camera.get(key[0], ()),
                                     prior=priors.get(key), max_hold=max_hold)
        return out

    # ---- Part B: the three questions a chatbot asks of a sparse history -----
    #
    # All three share the same preparation — resolve which zone is meant, pull
    # the rows in the window plus the one before it, and work out which stretches
    # the camera could not see. _zone_window does that once.

    def _zone_window(self, zone_id, t0, t1, camera_id=None):
        """(camera_id, rows, prior, outages) or an error dict.

        The error is returned rather than raised so the HTTP layer stays a thin
        translation and every caller gets the same wording.
        """
        matches = self.resolve_zone(zone_id, camera_id=camera_id)
        if not matches:
            return {"error": "not_found", "status": 404,
                    "message": f"no zone {zone_id!r}"
                               + (f" on camera {camera_id!r}" if camera_id else "")}
        if len(matches) > 1:
            # Deliberately not a guess. Zone ids repeat across cameras, so
            # ZONE-01 can be the lobby on one and the loading bay on another;
            # answering for whichever sorted first would be wrong in a way that
            # never surfaces downstream.
            return {"error": "ambiguous", "status": 409,
                    "message": f"zone {zone_id!r} exists on several cameras; "
                               f"pass camera_id",
                    "candidates": [{"camera_id": c, "zone_id": z} for c, z in matches]}

        cam, zid = matches[0]
        rows = self.store.zone_state_rows(t0, t1, camera_id=cam, zone_id=zid)
        priors = self.store.zone_state_prior(t0, camera_id=cam, zone_id=zid)
        return {"camera_id": cam, "zone_id": zid, "rows": rows,
                "prior": priors[0] if priors else None,
                "outages": self.camera_outages(cam, t0, t1)}

    def zone_series(self, zone_id, t0, t1, bucket_s, camera_id=None,
                    fields=None):
        """Bucketed history for one zone, gap-filled by holding each reading.

        The endpoint a chatbot needs most: "show me the last six hours" against
        a table that may contain four rows for that period.
        """
        from finblade.series import bucket_series, find_gaps

        ctx = self._zone_window(zone_id, t0, t1, camera_id=camera_id)
        if "error" in ctx:
            return ctx

        window = max(t1 - t0, 0.0)
        requested = float(bucket_s or 0)
        bucket = requested
        adjusted = False
        if bucket <= 0:
            bucket = max(window / 60.0, 1.0)
            adjusted = True
        if window / bucket > self.MAX_BUCKETS - 1:
            # MAX_BUCKETS - 1, not MAX_BUCKETS: buckets are aligned to the
            # epoch, so the first one almost always starts before the window
            # and one extra is needed to reach the end. Dividing by the cap
            # exactly produces MAX_BUCKETS + 1 points and overshoots the chart
            # tag's limit, which then trims from the FRONT.
            bucket = window / (self.MAX_BUCKETS - 1)
            adjusted = True

        max_hold = self.max_hold()
        fields = tuple(fields or self._TW_FIELDS)
        points = bucket_series(ctx["rows"], t0, t1, bucket, fields,
                               prior=ctx["prior"], offline=ctx["outages"],
                               max_hold=max_hold)
        gaps = find_gaps(ctx["rows"], t0, t1, offline=ctx["outages"],
                         max_hold=max_hold, prior=ctx["prior"])
        observed = window - sum(g["seconds"] for g in gaps)
        return {
            "zone_id": ctx["zone_id"], "camera_id": ctx["camera_id"],
            "from": t0, "to": t1,
            "bucket_seconds": bucket,
            # Echoed so a caller that asked for something impossible can see
            # what it got instead, rather than silently plotting the wrong
            # granularity against its own axis labels.
            "requested_bucket_seconds": requested or None,
            "bucket_adjusted": adjusted,
            "rows_in_window": len(ctx["rows"]),
            "coverage": round(max(observed, 0.0) / window, 4) if window else None,
            "gaps": gaps,
            "points": points,
        }

    def zone_at(self, zone_id, ts, camera_id=None):
        """The reading in force at one instant."""
        from finblade.series import state_at

        # A one-hour lookback is enough to find the governing row in almost
        # every case; zone_state_prior covers the rest without scanning.
        ctx = self._zone_window(zone_id, ts - 3600.0, ts, camera_id=camera_id)
        if "error" in ctx:
            return ctx

        row = state_at(ctx["rows"], ts, prior=ctx["prior"],
                       max_hold=self.max_hold())
        if row is None:
            return {"zone_id": ctx["zone_id"], "camera_id": ctx["camera_id"],
                    "at": ts, "state": None,
                    "reason": "no reading at or before this time"}
        # Inclusive at both ends on purpose. An outage still open at `ts` is
        # clipped to end exactly at `ts` by offline_intervals, so a half-open
        # test reports "camera fine" for the one case that matters most: the
        # camera is down right now. A recovery landing on the same instant is
        # then also called offline, which is the safe direction to be wrong in.
        offline = any(a <= ts <= b for a, b in ctx["outages"])
        return {"zone_id": ctx["zone_id"], "camera_id": ctx["camera_id"],
                "at": ts, "state": row,
                "camera_offline": offline,
                # Both flags say "do not present this as current fact". They are
                # separate because they have different causes: `stale` means the
                # reading outlived what a sample may speak for, `camera_offline`
                # means the event log says the camera was down at that moment.
                "trustworthy": not (row.get("stale") or offline)}

    def zone_duration(self, zone_id, t0, t1, camera_id=None, field=None,
                      op=None, value=None, status=None):
        """How long a condition held, and in how many separate episodes."""
        from finblade.series import (duration_where, field_predicate,
                                     status_predicate)

        if status:
            predicate = status_predicate(status)
            described = {"status": str(status).upper()}
        else:
            try:
                predicate = field_predicate(field, op, value)
            except ValueError as exc:
                return {"error": "bad_condition", "status": 422, "message": str(exc)}
            described = {"field": field, "op": op, "value": value}

        ctx = self._zone_window(zone_id, t0, t1, camera_id=camera_id)
        if "error" in ctx:
            return ctx

        out = duration_where(ctx["rows"], t0, t1, predicate, prior=ctx["prior"],
                             offline=ctx["outages"], max_hold=self.max_hold())
        out.update(zone_id=ctx["zone_id"], camera_id=ctx["camera_id"],
                   condition=described)
        return out

    def _zone_gaps(self, camera_id, zone_id, t0, t1):
        """Where a zone's coverage was missing, for the report.

        `coverage: 0.4` tells a reader the number is partial. It does not tell
        them whether the camera was down for one long stretch overnight or
        flapping all day, and those support different conclusions from the same
        average.
        """
        from finblade.series import find_gaps
        rows = self.store.zone_state_rows(t0, t1, camera_id=camera_id,
                                          zone_id=zone_id)
        priors = self.store.zone_state_prior(t0, camera_id=camera_id,
                                             zone_id=zone_id)
        return find_gaps(rows, t0, t1,
                         offline=self.camera_outages(camera_id, t0, t1),
                         max_hold=self.max_hold(),
                         prior=priors[0] if priors else None)

    def occupancy_report(self, t0, t1, camera_id=None, zone_id=None, generated_at=None):
        """Windowed occupancy report: per-zone stats enriched with alert counts,
        plus totals. Shared by the JSON/CSV endpoints and the R-08 scheduler."""
        from collections import Counter
        zones = self.store.zone_state_stats(t0, t1, camera_id=camera_id, zone_id=zone_id)
        alerts = self.store.list_alerts_history(t0, t1, camera_id=camera_id, limit=5000)
        by_zone = Counter(a.get("zone_id") for a in alerts if a.get("zone_id"))
        weighted = self.zone_time_weighted(t0, t1, camera_id=camera_id,
                                           zone_id=zone_id)
        # The averages a caller reads are now the time-weighted ones.
        #
        # Step 3 added these alongside the SQL AVG() so the two could be
        # compared on real data first; they agreed to within 0.003 on six of
        # eight live zones. Step 4 is what forces the promotion: AVG() over
        # rows is only correct while every row covers the same five seconds,
        # and as of this commit they do not. A quiet hour now writes 12
        # keepalive rows and a busy minute writes one, so averaging rows
        # equally would over-report the busy minute twelvefold.
        #
        # The raw SQL numbers stay, under "sampled", because they are what
        # every report generated before this commit contains and a reader
        # comparing across the boundary needs to see both.
        for z in zones:
            z["alert_count"] = by_zone.get(z.get("zone_id"), 0)
            tw = weighted.get((z.get("camera_id"), z.get("zone_id")))
            if not tw:
                continue
            z["sampled"] = {k: z.get(k) for k in
                            ("avg_occupancy", "avg_density", "avg_capacity_pct")}
            for key, field in (("avg_occupancy", "occupancy"),
                               ("avg_density", "density"),
                               ("avg_capacity_pct", "capacity_pct")):
                mean = tw["fields"][field]["mean"]
                # None means no observed time carried a value. Keep the sampled
                # figure rather than blanking the column — a report that shows
                # nothing where it used to show a number reads as a fault.
                if mean is not None:
                    z[key] = mean
            z["time_weighted"] = {
                "avg_occupancy": tw["fields"]["occupancy"]["mean"],
                "avg_density": tw["fields"]["density"]["mean"],
                "avg_capacity_pct": tw["fields"]["capacity_pct"]["mean"],
                "peak_occupancy": tw["fields"]["occupancy"]["peak"],
                "coverage": tw["coverage"],
                "observed_seconds": tw["observed_seconds"],
            }
            # Surfaced at zone level so the CSV and the dashboard can qualify a
            # figure the camera only half observed, instead of presenting an
            # average of four hours as if it covered twenty-four.
            z["coverage"] = tw["coverage"]
            z["gaps"] = self._zone_gaps(z.get("camera_id"), z.get("zone_id"), t0, t1)
        return {
            "from": t0, "to": t1,
            "generated_at": time.time() if generated_at is None else generated_at,
            "zones": zones,
            "totals": {
                "zones": len(zones),
                # The window's worst coverage, not its average. A report whose
                # zones range from 1.00 to 0.05 is not "52% observed" — one
                # camera was down and any conclusion drawn from it is unsafe,
                # which the mean would hide.
                "min_coverage": min((z["coverage"] for z in zones
                                     if z.get("coverage") is not None), default=None),
                "peak_total_occupancy": sum(int(z.get("peak_occupancy") or 0) for z in zones),
                "peak_density": max((float(z.get("peak_density") or 0) for z in zones),
                                    default=0.0),
                "total_alerts": len(alerts),
            },
        }

    def generate_report(self, t0, t1, kind="ondemand", camera_id=None) -> dict:
        rep = self.occupancy_report(t0, t1, camera_id=camera_id)
        rep["kind"] = kind
        rep["report_id"] = self.store.save_report(rep)
        return rep

    def list_reports(self, limit=100):
        return self.store.list_reports(limit)

    def get_report(self, report_id):
        return self.store.get_report(report_id)

    # -- zones (editor save/load) --
    def save_zones(self, payload: dict) -> Tuple[int, dict]:
        ok, errors = validate_zones(payload)
        if not ok:
            return 422, {"saved": False, "errors": errors}
        self.store.save_zones(payload["camera_id"], payload["zones"])
        # Retyping a zone as a door (or away from one) must take effect on the
        # next event, not up to the policy TTL later.
        self.invalidate_door_policy()
        # Same for a zone's physical area. Without this the registry keeps the
        # previous mapping for up to _AREA_RELOAD_S, so zone state arriving in
        # the seconds right after an operator maps two cameras to one room is
        # still attributed the old way — the room reads 2 and then settles to
        # 1, which looks exactly like the de-duplication being unreliable.
        self._areas_loaded = 0.0
        return 200, {"saved": True, "camera_id": payload["camera_id"],
                     "count": len(payload["zones"])}

    def list_zones(self, camera_id=None):
        return self.store.list_zones(camera_id)

    # -- physical areas -----------------------------------------------------
    #
    # A camera zone is one viewpoint; a physical area is the room. Occupancy
    # for a room is the number of DISTINCT people its cameras can see, so a
    # person standing in the overlap of two cameras counts once. See
    # finblade/areas.py for the counting rules and why summing is wrong.

    _AREA_RELOAD_S = 5.0

    def _area_tracker(self):
        """The AreaOccupancy instance, with its zone->area map kept current.

        The map is refreshed on a short interval rather than per post: an
        operator remapping a zone in the editor should take effect within
        seconds, but re-reading the zone table on every 5s post from every
        camera is needless work.
        """
        from finblade.areas import AreaOccupancy, AreaRegistry, area_from_dict
        now = time.time()
        tracker = getattr(self, "_areas", None)
        if tracker is None:
            tracker = self._areas = AreaOccupancy(AreaRegistry())
            self._areas_loaded = 0.0
        if (now - getattr(self, "_areas_loaded", 0.0)) > self._AREA_RELOAD_S:
            reg = AreaRegistry([area_from_dict(a) for a in self.store.list_areas()])
            reg.load_zone_rows(self.store.list_zones())
            tracker.registry = reg
            self._areas_loaded = now
        return tracker

    def _observe_area(self, payload: dict) -> None:
        occupants = payload.get("occupants")
        if occupants is None:
            # Worker does not report identities. Deliberately NOT synthesised
            # from the count: inventing per-person keys here would make two
            # cameras' anonymous "1"s look like two different people, which is
            # exactly the double-count this feature exists to remove.
            return
        tracker = self._area_tracker()
        tracker.observe(payload.get("camera_id"), payload["zone_id"],
                        occupants, payload.get("ts") or time.time())

    def area_states(self, now=None):
        now = now if now is not None else time.time()
        tracker = self._area_tracker()
        tracker.tick(now)
        return tracker.snapshot(now)

    def area_state(self, area_id, now=None):
        now = now if now is not None else time.time()
        tracker = self._area_tracker()
        if area_id not in tracker.registry.area_ids():
            return None
        return tracker.state(area_id, now)

    def save_area(self, payload: dict):
        if not isinstance(payload, dict) or not payload.get("area_id"):
            return 422, {"saved": False, "errors": ["area_id is required"]}
        self.store.save_area(payload)
        self._areas_loaded = 0.0          # take effect on the next read
        return 200, {"saved": True, "area_id": payload["area_id"]}

    def list_areas(self):
        return self.store.list_areas()

    def delete_area(self, area_id):
        gone = self.store.delete_area(area_id)
        self._areas_loaded = 0.0
        return gone

    # -- organisation hierarchy: Region -> City -> Branch --
    # Referential checks live here, not only in the database, so the in-memory
    # backend refuses the same writes Postgres would and the HTTP layer can say
    # 422 (bad parent) or 409 (has children) instead of surfacing an
    # IntegrityError. finblade/org.py holds the pure logic.
    def org_index(self) -> dict:
        """The raw rows plus tenant meta — what the editors and the seed
        script want, with no live counts attached."""
        return {"meta": self.store.get_org_meta(),
                "regions": self.store.list_regions(),
                "cities": self.store.list_cities(),
                "branches": self.store.list_branches()}

    def org_tree(self, cameras: List[dict] = (), zones: List[dict] = (),
                 alerts: List[dict] = ()) -> dict:
        """The nested tree with camera / zone / alert counts rolled up per
        level. `cameras` must already carry effective_state (the API's
        _camera_list does that) — this does not recompute liveness."""
        idx = self.org_index()
        tree = _org.build_tree(idx["regions"], idx["cities"], idx["branches"],
                               cameras=list(cameras), zones=list(zones),
                               alerts=list(alerts))
        tree["meta"] = idx["meta"]
        return tree

    def branches_in_scope(self, region_id=None, city_id=None, branch_id=None):
        """Set of site_ids a read is narrowed to, or None for no scope."""
        if not (region_id or city_id or branch_id):
            return None
        return _org.branches_in_scope(self.store.list_cities(),
                                      self.store.list_branches(),
                                      region_id=region_id, city_id=city_id,
                                      branch_id=branch_id)

    def branch_known(self, site_id) -> bool:
        return bool(site_id) and any(b["branch_id"] == site_id
                                     for b in self.store.list_branches())

    def save_region(self, payload: dict) -> Tuple[int, dict]:
        row, errors = _org.validate_region(payload or {})
        if errors:
            return 422, {"saved": False, "errors": errors}
        self.store.save_region(row)
        return 200, {"saved": True, "region_id": row["region_id"]}

    def save_city(self, payload: dict) -> Tuple[int, dict]:
        row, errors = _org.validate_city(
            payload or {}, [r["region_id"] for r in self.store.list_regions()])
        if errors:
            return 422, {"saved": False, "errors": errors}
        self.store.save_city(row)
        return 200, {"saved": True, "city_id": row["city_id"]}

    def save_branch(self, payload: dict) -> Tuple[int, dict]:
        row, errors = _org.validate_branch(
            payload or {}, [c["city_id"] for c in self.store.list_cities()])
        if errors:
            return 422, {"saved": False, "errors": errors}
        self.store.save_branch(row)
        self._geo_loaded = 0.0             # fences follow the branch table
        return 200, {"saved": True, "branch_id": row["branch_id"]}

    def _delete_node(self, level: str, node_id: str, exists: bool,
                     children: int, deleter) -> Tuple[int, dict]:
        if not exists:
            return 404, {"deleted": False, "error": f"unknown {level}",
                         f"{level}_id": node_id}
        if children:
            return 409, {"deleted": False, f"{level}_id": node_id,
                         "error": f"{level} still has {children} child(ren); "
                                  "delete or move them first"}
        return 200, {"deleted": bool(deleter(node_id)), f"{level}_id": node_id}

    def delete_region(self, region_id: str) -> Tuple[int, dict]:
        rid = str(region_id)
        return self._delete_node(
            "region", rid,
            any(r["region_id"] == rid for r in self.store.list_regions()),
            sum(1 for c in self.store.list_cities() if c.get("region_id") == rid),
            self.store.delete_region)

    def delete_city(self, city_id: str) -> Tuple[int, dict]:
        cid = str(city_id)
        return self._delete_node(
            "city", cid,
            any(c["city_id"] == cid for c in self.store.list_cities()),
            sum(1 for b in self.store.list_branches() if b.get("city_id") == cid),
            self.store.delete_city)

    def delete_branch(self, branch_id: str) -> Tuple[int, dict]:
        bid = str(branch_id)
        # Cameras are the branch's children here. They are not deleted with
        # it — a camera is a running pipeline, not an org-chart entry — but a
        # branch that still owns cameras is not removed either, or those
        # cameras would silently drop out of every regional total.
        return self._delete_node(
            "branch", bid,
            any(b["branch_id"] == bid for b in self.store.list_branches()),
            sum(1 for c in self.store.list_cameras() if c.get("site_id") == bid),
            self.store.delete_branch)

    def set_org_meta(self, payload: dict) -> Tuple[int, dict]:
        if not isinstance(payload, dict):
            return 422, {"saved": False, "errors": ["object expected"]}
        allowed = {k: payload.get(k) for k in ("tenant_name", "tenant_country",
                                               "tenant_short")
                   if k in payload}
        if not allowed:
            return 422, {"saved": False,
                         "errors": ["nothing to set: tenant_name, tenant_country, tenant_short"]}
        self.store.set_org_meta(allowed)
        return 200, {"saved": True, "meta": self.store.get_org_meta()}

    def import_org(self, payload: dict) -> Tuple[int, dict]:
        """Load a whole tree in one call. Idempotent: rows are upserted by id,
        nothing is deleted, so re-running a seed file is safe."""
        if not isinstance(payload, dict):
            return 422, {"imported": False, "errors": ["object expected"]}
        regions, cities, branches, errors = _org.flatten_import(payload)
        if errors:
            return 422, {"imported": False, "errors": errors}
        for r in regions:
            self.store.save_region(r)
        for c in cities:
            self.store.save_city(c)
        for b in branches:
            self.store.save_branch(b)
        self._geo_loaded = 0.0
        tenant = payload.get("tenant") or {}
        if isinstance(tenant, dict) and tenant:
            self.store.set_org_meta({
                "tenant_name": tenant.get("name"),
                "tenant_country": tenant.get("country"),
                "tenant_short": tenant.get("short")})
        return 200, {"imported": True, "regions": len(regions),
                     "cities": len(cities), "branches": len(branches)}

    # -- GPS trackers (finblade/gps.py) --
    # A tracker is a vehicle or an asset. The ingest path is deliberately
    # permissive about WHO reports — an unregistered id is stored and shown
    # as "unregistered" so a phone set up in the field before the office
    # adds it is not silently dropped — and strict about WHAT: no fix, no
    # row.
    _TRACKER_KINDS = ("GPS", "BLE_TAG")
    _ASSET_TYPES = ("VEHICLE", "DEVICE", "SAMPLE_BOX")
    TRACKER_SILENT_S = float(os.environ.get("FINBLADE_TRACKER_SILENT_S",
                                            _trk.DEFAULT_SILENT_S))

    def _geofences(self) -> _trk.GeofenceEngine:
        now = time.time()
        if now - self._geo_loaded > 30.0:
            self._geo.set_fences(_trk.fences_from_branches(self.store.list_branches()))
            self._geo_loaded = now
        if not self._geo_restored:
            for live in self.store.latest_positions():
                self._geo.restore(live["tracker_id"], live.get("at_branch_id"),
                                  live.get("at_since"))
            self._geo_restored = True
        return self._geo

    def register_tracker(self, payload: dict) -> Tuple[int, dict]:
        if not isinstance(payload, dict):
            return 422, {"saved": False, "errors": ["object expected"]}
        errors = []
        tid = _trk.norm_tracker_id(payload.get("tracker_id"))
        if not tid:
            errors.append("tracker_id is required: letters, digits, '_', '-', '.', ':'")
        kind = str(payload.get("kind") or "GPS").upper()
        if kind not in self._TRACKER_KINDS:
            errors.append(f"kind must be one of {', '.join(self._TRACKER_KINDS)}")
        atype = str(payload.get("asset_type") or "VEHICLE").upper()
        if atype not in self._ASSET_TYPES:
            errors.append(f"asset_type must be one of {', '.join(self._ASSET_TYPES)}")
        home = payload.get("home_branch_id") or None
        if home and not self.branch_known(home):
            errors.append(f"unknown home_branch_id {home!r}")
        if errors:
            return 422, {"saved": False, "errors": errors}
        self.store.save_tracker({
            "tracker_id": tid, "name": (payload.get("name") or tid),
            "kind": kind, "device_ref": payload.get("device_ref") or None,
            "asset_type": atype, "asset_label": payload.get("asset_label") or None,
            "home_branch_id": home,
            "enabled": payload.get("enabled", True) is not False})
        return 200, {"saved": True, "tracker_id": tid}

    def delete_tracker(self, tracker_id: str) -> Tuple[int, dict]:
        ok = self.store.delete_tracker(tracker_id)
        self._tracker_silent.pop(str(tracker_id), None)
        return (200 if ok else 404), {"deleted": ok, "tracker_id": tracker_id}

    def trackers(self, now: Optional[float] = None) -> List[dict]:
        """Registered trackers merged with their latest position, plus any
        unregistered id that has reported. `site_id` is the home branch, so
        the Region/City/Branch scope applies to vehicles too."""
        now = time.time() if now is None else now
        reg = {t["tracker_id"]: dict(t) for t in self.store.list_trackers()}
        live = {p["tracker_id"]: p for p in self.store.latest_positions()}
        out = []
        for tid in sorted(set(reg) | set(live)):
            t = reg.get(tid) or {"tracker_id": tid, "name": tid, "kind": "GPS",
                                 "asset_type": "VEHICLE", "registered": False}
            t.setdefault("registered", True)
            t["site_id"] = t.get("home_branch_id")
            p = live.get(tid)
            if p:
                t["position"] = {k: p.get(k) for k in ("ts", "lat", "lon", "speed_kmh",
                                                       "heading", "accuracy_m",
                                                       "battery_pct", "positions")}
                t["at_branch_id"] = p.get("at_branch_id")
                t["at_since"] = p.get("at_since")
                silent = _trk.silent_for(p.get("ts"), now)
            else:
                t["position"] = None
                t["at_branch_id"] = None
                silent = None
            t["seconds_since_seen"] = round(silent, 1) if silent is not None else None
            t["state"] = ("NEVER_SEEN" if silent is None
                          else "OFFLINE" if silent > self.TRACKER_SILENT_S
                          else "MOVING" if (p.get("speed_kmh") or 0) > 3.0
                          else "STOPPED")
            out.append(t)
        return out

    def ingest_position(self, p: _trk.Position) -> Tuple[int, dict]:
        """Store one position and run it through the branch geofences."""
        geo = self._geofences()
        transitions = geo.observe(p)
        at, since = geo.at(p.tracker_id)
        self.store.save_position(p.to_dict(), at_branch_id=at, at_since=since)
        emitted = []
        for tr in transitions:
            evt = new_event(TRACKER_ARRIVED if tr.kind == "ARRIVED" else TRACKER_DEPARTED,
                            p.tracker_id, tr.branch_id, tr.ts,
                            tracker_id=p.tracker_id, branch_id=tr.branch_id,
                            distance_m=round(tr.distance_m, 1),
                            **({"dwell_s": round(tr.dwell_s, 1)} if tr.dwell_s is not None else {}))
            code, _ = self.ingest_event(evt)
            if code == 202:
                emitted.append(evt["event_type"])
                self._notify("tracker.arrived" if tr.kind == "ARRIVED" else "tracker.departed",
                             tracker_event=dict(evt, site_id=tr.branch_id))
        # A report clears an open silence alert, same as a camera recovering.
        self._tracker_recovered(p.tracker_id, p.ts)
        return 202, {"accepted": True, "tracker_id": p.tracker_id,
                     "at_branch_id": at, "events": emitted}

    def tracker_track(self, tracker_id: str, t0: float, t1: float,
                      limit: int = 5000) -> List[dict]:
        return self.store.positions_range(tracker_id, t0, t1, limit=limit)

    # R-12: a tracker that stops reporting. Same shape as R-07 for cameras
    # (raise once, auto-resolve on recovery) so the alert feed treats them
    # alike. Driven by a loop in app.py.
    def check_silent_trackers(self, now: Optional[float] = None) -> List[str]:
        now = time.time() if now is None else now
        fired = []
        for t in self.trackers(now):
            tid = t["tracker_id"]
            if t.get("enabled") is False or t["state"] == "NEVER_SEEN":
                continue
            if t["state"] == "OFFLINE" and not self._tracker_silent.get(tid):
                self._tracker_silent[tid] = True
                mins = int(self.TRACKER_SILENT_S // 60)
                self.raise_alert({
                    "rule_id": "R-12", "severity": "AMBER", "kind": "FIRE",
                    "message": f"tracker {t.get('name') or tid} silent >{mins} min"
                               + (f" (last at {t['at_branch_id']})" if t.get("at_branch_id") else ""),
                    "camera_id": tid, "site_id": t.get("home_branch_id"), "ts": now})
                fired.append(tid)
        return fired

    def _tracker_recovered(self, tracker_id: str, now: float) -> None:
        if not self._tracker_silent.get(tracker_id):
            return
        self._tracker_silent[tracker_id] = False
        for a in self.store.list_alerts(unacked_only=False):
            if a.get("rule_id") == "R-12" and a.get("camera_id") == tracker_id:
                self.resolve(str(a.get("alert_id")), "RESOLVED", "system-recovery",
                             now, note="tracker reporting again")
        self.raise_alert({"rule_id": "R-12", "severity": "INFO", "kind": "CLEAR",
                          "message": f"tracker {tracker_id} reporting again",
                          "camera_id": tracker_id, "ts": now})

    # -- appearance sightings and search (finblade/attributes.py) --
    SIGHTING_ATTRS = ("upper_colour", "lower_colour", "headwear", "mask", "bag", "outerwear")

    def _save_sighting(self, evt: dict) -> None:
        attrs = evt.get("attributes") or {}
        row = {"event_id": evt.get("event_id"), "ts": float(evt.get("timestamp") or 0),
               "site_id": evt.get("site_id"), "camera_id": evt.get("camera_id"),
               "zone_id": evt.get("zone_id"), "person_ref": evt.get("person_ref"),
               "global_ref": evt.get("global_ref"), "confidences": evt.get("confidences") or {},
               "samples": evt.get("samples"), "description": evt.get("description"),
               "frame": evt.get("frame"),
               "extra": {k: v for k, v in attrs.items() if k not in self.SIGHTING_ATTRS}}
        for k in self.SIGHTING_ATTRS:
            # An attribute the camera does not judge (attributes.disable) is
            # "unknown", same as one it judged and could not decide — the
            # search treats both as "no answer", and the column never holds
            # a null that reads differently from every other row.
            row[k] = attrs.get(k) or "unknown"
        self.store.save_sighting(row)

    def find_people(self, filters: dict, t0: float, t1: float, site_ids=None,
                    camera_id: str = None, actor: str = "api", limit: int = 500,
                    near: bool = True) -> dict:
        """Sightings matching a description, GROUPED BY PERSON.

        A person seen on three cameras is one result with a timeline, because
        that is the question — "where did they go" — and because three rows
        for one person reads as three people. Unresolved sightings (no
        global_ref) stay separate: merging them would be a guess. Every call
        is written to the audit table; this is an incident tool.

        With `near` (the default) a colour also matches its confusable
        neighbours (finblade.attributes.NEAR_COLOURS): "white" finds the
        person the tagger stored as "grey". Exact hits rank first and every
        hit says which it is, so the operator can tell a match from a maybe.
        """
        from finblade.attributes import near_labels
        filters = {k: str(v) for k, v in (filters or {}).items() if v}
        accept = {k: (near_labels(k, v) if near else (v,)) for k, v in filters.items()}
        rows = self.store.search_sightings(t0, t1, filters=accept, site_ids=site_ids,
                                           camera_id=camera_id, limit=limit)
        attr_cols = set(self.SIGHTING_ATTRS)

        def _match(r: dict) -> str:
            for k, v in filters.items():
                have = r.get(k) if k in attr_cols else (r.get("extra") or {}).get(k)
                if have != v:
                    return "near"
            return "exact"

        groups: Dict[str, dict] = {}
        for r in rows:
            key = r.get("global_ref") or f"{r.get('camera_id')}:{r.get('person_ref')}"
            g = groups.setdefault(key, {"person": key, "resolved": bool(r.get("global_ref")),
                                        "description": r.get("description"), "match": "near",
                                        "first_seen": r["ts"], "last_seen": r["ts"],
                                        "cameras": set(), "sites": set(), "sightings": []})
            g["first_seen"] = min(g["first_seen"], r["ts"]); g["last_seen"] = max(g["last_seen"], r["ts"])
            g["cameras"].add(r.get("camera_id")); g["sites"].add(r.get("site_id"))
            m = _match(r)
            if m == "exact":
                g["match"] = "exact"
            # sighting_id is the contract for fetching the crop
            # (GET /api/v1/search/sightings/{id}/crop); `frame` stays for the
            # UI's <img> but is a filesystem-shaped path, not a promise.
            g["sightings"].append(dict({k: r.get(k) for k in ("ts", "site_id", "camera_id", "zone_id",
                                                               "description", "frame", "confidences",
                                                               "samples")}, match=m,
                                       sighting_id=r.get("event_id"),
                                       # a clickable, expiring, key-free link to the crop
                                       crop_url=_crop_url(r)))
        out = []
        for g in groups.values():
            g["cameras"] = sorted(c for c in g["cameras"] if c)
            g["sites"] = sorted(s for s in g["sites"] if s)
            g["sightings"].sort(key=lambda s: s["ts"])
            out.append(g)
        out.sort(key=lambda g: (g["match"] != "exact", -g["last_seen"]))
        self.store.record_search(actor, dict(filters, **{"from": t0, "to": t1,
                                                          "site_ids": sorted(site_ids) if site_ids else None,
                                                          "camera_id": camera_id, "near": bool(near)}),
                                 len(out), time.time())
        expanded = {k: list(v[1:]) for k, v in accept.items() if len(v) > 1}
        return {"people": out, "count": len(out), "sightings": len(rows),
                "exact": sum(1 for g in out if g["match"] == "exact"),
                "filters": filters, "near_colours": expanded, "from": t0, "to": t1}

    def correct_sighting(self, sighting_id: str, attribute: str, value: str,
                         actor: str = "operator") -> Tuple[int, dict]:
        """A human looked at the crop and overrode one tag.

        The usual correction is "unknown" — the tagger was wrong and nobody
        wants to replace one guess with another. A real label is accepted
        only from the current vocabulary. The description is rewritten and
        the correction is written to search_audit with who did it, so a
        search hit and the audit trail always agree on where a label came
        from. The confidence stays as the model reported it: the record says
        what the model thought and that a person disagreed.
        """
        from finblade.attributes import DEFAULT_VOCABULARY, FORBIDDEN_ATTRIBUTES, describe
        attribute, value = str(attribute), str(value)
        if attribute in FORBIDDEN_ATTRIBUTES:
            return 422, {"error": "forbidden attribute", "attribute": attribute}
        allowed = set(DEFAULT_VOCABULARY.get(attribute, {}).keys())
        if attribute not in self.SIGHTING_ATTRS and attribute not in DEFAULT_VOCABULARY:
            return 422, {"error": "unknown attribute", "attribute": attribute}
        if value != "unknown" and allowed and value not in allowed:
            return 422, {"error": "value not in vocabulary", "attribute": attribute,
                         "allowed": sorted(allowed) + ["unknown"]}
        row = self.store.get_sighting(sighting_id)
        if row is None:
            return 404, {"error": "unknown sighting", "sighting_id": sighting_id}
        previous = row.get(attribute) if attribute in self.SIGHTING_ATTRS else (row.get("extra") or {}).get(attribute)
        labels = {k: row.get(k) for k in self.SIGHTING_ATTRS}
        labels.update(row.get("extra") or {})
        labels[attribute] = value
        desc = describe({k: v for k, v in labels.items() if v})
        if not self.store.correct_sighting(sighting_id, {attribute: value}, desc):
            return 404, {"error": "unknown sighting", "sighting_id": sighting_id}
        self.store.record_search(actor, {"correction": sighting_id, "attribute": attribute,
                                         "from": previous, "to": value}, 1, time.time())
        return 200, {"ok": True, "sighting_id": sighting_id, "attribute": attribute,
                     "from": previous, "to": value, "description": desc}

    # -- outbound webhooks (finblade/webhooks.py) --
    def save_webhook(self, payload: dict, existing_id: str = None) -> Tuple[int, dict]:
        from finblade import webhooks as _wh
        row, errors = _wh.validate_subscription(payload or {}, existing_id=existing_id)
        if errors:
            return 422, {"saved": False, "errors": errors}
        if existing_id and not (payload or {}).get("secret"):
            # Editing keeps the existing secret; a new one is only minted when
            # asked for, or the receiver would silently stop verifying.
            old = next((w for w in self.store.list_webhooks() if w["webhook_id"] == existing_id), None)
            if old:
                row["secret"] = old["secret"]
        for k in ("region_id", "city_id", "branch_id"):
            if row.get(k) and self.branches_in_scope(**{k: row[k]}) == set():
                return 422, {"saved": False, "errors": [f"unknown {k} {row[k]!r}"]}
        self.store.save_webhook(row)
        out = _wh.public_view(row)
        if not existing_id or (payload or {}).get("secret"):
            out["secret"] = row["secret"]       # shown in full exactly once
        return 200, {"saved": True, "webhook": out}

    def list_webhooks(self) -> List[dict]:
        from finblade import webhooks as _wh
        return [_wh.public_view(w) for w in self.store.list_webhooks()]

    def delete_webhook(self, webhook_id: str) -> Tuple[int, dict]:
        ok = self.store.delete_webhook(webhook_id)
        return (200 if ok else 404), {"deleted": ok, "webhook_id": webhook_id}

    # -- alerts --
    def site_for_camera(self, camera_id) -> str:
        """The site a camera belongs to, or None.

        Alerts and zone states arrive from workers that know their camera but do
        not always carry a site. Deriving it here means one CCTV deployment can
        feed a multi-site platform without every worker being reconfigured, and
        without the platform having to join camera data to attribute a record.
        """
        if not camera_id:
            return None
        for c in self.store.list_cameras():
            if c.get("camera_id") == camera_id:
                return c.get("site_id")
        return None

    def raise_alert(self, alert: dict) -> str:
        if not alert.get("site_id"):
            site = self.site_for_camera(alert.get("camera_id"))
            if site:
                alert = dict(alert, site_id=site)
        alert_id = self.store.save_alert(alert)
        # Fan out AFTER the row exists so the envelope carries the real id.
        # A CLEAR is the INFO companion of a recovered condition, not a new
        # problem — a different event so a workflow can close what it opened.
        event = "alert.cleared" if str(alert.get("kind", "FIRE")).upper() == "CLEAR" else "alert.raised"
        self._notify(event, alert=dict(alert, alert_id=alert_id, status="OPEN"))
        return alert_id

    def _notify(self, event: str, **kw) -> None:
        """Webhook fan-out must never break the caller: a bad subscription
        row or a store hiccup is logged, and the alert stands."""
        try:
            self.webhooks.notify(event, **kw)
        except Exception:                                   # noqa: BLE001
            import logging
            logging.getLogger("finblade.webhooks").exception("webhook notify failed for %s", event)

    def _webhook_context(self, rec: dict) -> dict:
        """What a workflow needs to act without a second call: the branch
        (with its city and region), the camera, and the zone's live state."""
        out = {}
        site = rec.get("site_id") or rec.get("branch_id")
        if site:
            idx = self.org_index()
            b = next((x for x in idx["branches"] if x["branch_id"] == site), None)
            if b:
                c = next((x for x in idx["cities"] if x["city_id"] == b.get("city_id")), {})
                r = next((x for x in idx["regions"] if x["region_id"] == c.get("region_id")), {})
                out["branch"] = {"branch_id": b["branch_id"], "name": b.get("name"),
                                 "branch_type": b.get("branch_type"), "lat": b.get("lat"),
                                 "lon": b.get("lon"), "city_id": c.get("city_id"),
                                 "city": c.get("name"), "region_id": r.get("region_id"),
                                 "region": r.get("name")}
            else:
                out["branch"] = {"branch_id": site, "name": None, "unassigned": True}
        cam_id = rec.get("camera_id")
        if cam_id:
            cam = next((c for c in self.store.list_cameras() if c.get("camera_id") == cam_id), None)
            if cam:
                # Never the source: it can hold an RTSP password.
                out["camera"] = {k: cam.get(k) for k in ("camera_id", "name", "site_id", "state",
                                                          "people_in_view", "last_seen")}
        zid = rec.get("zone_id")
        if zid and cam_id:
            z = next((z for z in self.store.latest_zone_states()
                      if z.get("zone_id") == zid and z.get("camera_id") == cam_id), None)
            if z:
                out["zone"] = {k: z.get(k) for k in ("zone_id", "zone_name", "zone_type", "restricted",
                                                     "occupancy", "density", "capacity_pct", "status",
                                                     "physical_area_id")}
        return out

    def get_alert(self, alert_id: str):
        """One alert by id, open or closed. None if unknown."""
        target = str(alert_id)
        for a in self.store.list_alerts(unacked_only=False):
            if str(a.get("alert_id")) == target:
                return a
        # FAR_FUTURE, not now+86400: an alert stamped by a worker whose clock
        # runs ahead is still an alert, and the id is exact.
        for a in self.store.list_alerts_history(0, self.FAR_FUTURE, limit=100000):
            if str(a.get("alert_id")) == target:
                return a
        return None

    # Filter names accepted by list_alerts, matched case-insensitively against
    # the alert field of the same name.
    _ALERT_FILTERS = ("severity", "status", "zone_id", "camera_id", "rule_id",
                      "site_id")

    def list_alerts(self, unacked_only: bool = False, **filters) -> List[dict]:
        """Active alerts, optionally narrowed.

        Filtering happens here rather than in each store so every backend
        behaves identically; the active set is small by construction (resolved
        and dismissed alerts drop out), so this is not the query to optimise.
        """
        rows = self.store.list_alerts(unacked_only=unacked_only)
        for field in self._ALERT_FILTERS:
            wanted = filters.get(field)
            if wanted in (None, ""):
                continue
            wanted = str(wanted).upper()
            rows = [a for a in rows if str(a.get(field) or "").upper() == wanted]
        return rows

    def acknowledge(self, alert_id: str, who: str, ts: float) -> Tuple[int, dict]:
        if not who:
            return 400, {"acknowledged": False, "error": "acknowledged_by required"}
        ok = self.store.acknowledge_alert(alert_id, who, ts)
        if not ok:
            return 409, {"acknowledged": False,
                         "error": "unknown or already-acknowledged alert"}
        rec = self.get_alert(alert_id)
        if rec:
            self._notify("alert.acknowledged", alert=rec)
        return 200, {"acknowledged": True, "alert_id": alert_id,
                     "acknowledged_by": who, "acknowledged_at": ts}

    def clear_alerts(self, scope: str = "closed", delete_frames: bool = True
                     ) -> Tuple[int, dict]:
        """Delete alerts and, optionally, the snapshot files they own.

        scope "closed" (default) removes only RESOLVED/DISMISSED alerts, so an
        operator cannot wipe something still needing attention with one click.
        "all" removes everything.

        Frame deletion is deliberately paranoid about paths: refs come from the
        database as URL paths like "/bookmarks/bm_CAM_00001.jpg", and are only
        unlinked after resolving to a real file that sits INSIDE the bookmarks
        directory. A ref of "../../etc/passwd" resolves outside and is skipped.
        """
        import os

        scope = (scope or "closed").lower()
        if scope not in ("closed", "all"):
            return 400, {"ok": False, "error": "scope must be 'closed' or 'all'"}

        count, frames = self.store.delete_alerts(scope)

        removed = failed = 0
        if delete_frames and frames:
            root = os.path.abspath(os.path.join(
                os.path.dirname(__file__), "..", "..", "evidence", "bookmarks"))
            for ref in frames:
                name = str(ref).split("/")[-1]
                path = os.path.abspath(os.path.join(root, name))
                if not path.startswith(root + os.sep):
                    failed += 1          # escaped the bookmarks dir; leave it
                    continue
                try:
                    os.remove(path)
                    removed += 1
                except FileNotFoundError:
                    pass                 # already gone: not an error
                except OSError:
                    failed += 1
        return 200, {"ok": True, "scope": scope, "alerts_deleted": count,
                     "frames_deleted": removed, "frames_failed": failed}

    def orphaned_frames(self) -> dict:
        """Snapshot files on disk that no alert references any more.

        Alerts deleted before this endpoint existed left their JPEGs behind, and
        so does any alert removed directly from the database.
        """
        import os

        root = os.path.abspath(os.path.join(
            os.path.dirname(__file__), "..", "..", "evidence", "bookmarks"))
        if not os.path.isdir(root):
            return {"orphans": 0, "bytes": 0, "dir": root}
        referenced = set()
        for a in self.store.list_alerts_history(0, 9_999_999_999, limit=100000):
            if a.get("frame"):
                referenced.add(str(a["frame"]).split("/")[-1])
        orphans, total = 0, 0
        for name in os.listdir(root):
            if name in referenced:
                continue
            try:
                total += os.path.getsize(os.path.join(root, name))
                orphans += 1
            except OSError:
                pass
        return {"orphans": orphans, "bytes": total,
                "mb": round(total / (1024 * 1024), 1), "dir": root}

    def delete_orphaned_frames(self) -> Tuple[int, dict]:
        import os

        info = self.orphaned_frames()
        root = info["dir"]
        if not os.path.isdir(root):
            return 200, {"ok": True, "frames_deleted": 0}
        referenced = set()
        for a in self.store.list_alerts_history(0, 9_999_999_999, limit=100000):
            if a.get("frame"):
                referenced.add(str(a["frame"]).split("/")[-1])
        removed = 0
        for name in os.listdir(root):
            if name in referenced:
                continue
            try:
                os.remove(os.path.join(root, name))
                removed += 1
            except OSError:
                pass
        return 200, {"ok": True, "frames_deleted": removed,
                     "mb_freed": info.get("mb", 0)}

    def resolve(self, alert_id: str, action: str, who: str, ts: float,
                note: str = None) -> Tuple[int, dict]:
        """Close an alert: action 'RESOLVED' (handled) or 'DISMISSED' (false alarm),
        with an optional operator note."""
        action = (action or "").upper()
        if action not in ("RESOLVED", "DISMISSED"):
            return 400, {"ok": False, "error": "action must be RESOLVED or DISMISSED"}
        if not who:
            return 400, {"ok": False, "error": "resolved_by required"}
        ok = self.store.update_alert(alert_id, action, who, ts, note)
        if not ok:
            return 409, {"ok": False, "error": "unknown or already-closed alert"}
        rec = self.get_alert(alert_id)
        if rec:
            self._notify("alert.resolved", alert=rec)
        return 200, {"ok": True, "alert_id": alert_id, "status": action,
                     "resolved_by": who, "resolved_at": ts, "note": note}

    # -- dashboard reads --
    def zone_states(self) -> List[dict]:
        return self.store.latest_zone_states()

    def zone_state_range(self, zone_id: str, t0: float, t1: float) -> List[dict]:
        return self.store.zone_state_range(zone_id, t0, t1)
