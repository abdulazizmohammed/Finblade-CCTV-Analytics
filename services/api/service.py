"""Framework-agnostic ingest/query service.

All the API's business logic lives here so it is unit-testable without FastAPI,
Redis, or Postgres. app.py is a thin HTTP adapter over this class.
"""

import os
import time
from typing import List, Optional, Tuple

from finblade.emission import DEFAULT_KEEPALIVE, StateWriteGate

from .schema import validate_ingest, validate_zone_state, validate_zones
from .store import Store


class IngestService:
    def __init__(self, store: Store, bus=None, state_gate=None):
        self.store = store
        self.bus = bus  # optional event bus with .publish(evt); None = skip
        # Write-on-change for zone_state_ts. See finblade/emission.py; set
        # FINBLADE_STATE_WRITES=always to restore a row per post.
        self.state_gate = state_gate if state_gate is not None else StateWriteGate(
            os.environ.get("FINBLADE_STATE_WRITES", "change"),
            float(os.environ.get("FINBLADE_STATE_KEEPALIVE", DEFAULT_KEEPALIVE) or 0))

    # -- POST /api/v1/events/ingest --
    def ingest_event(self, payload: dict) -> Tuple[int, dict]:
        ok, errors = validate_ingest(payload)
        if not ok:
            return 422, {"accepted": False, "errors": errors}
        self.store.save_event(payload)
        # Any event from a camera counts as a heartbeat for offline detection.
        self.store.mark_camera_seen(payload.get("camera_id"), payload.get("timestamp"),
                                    payload.get("site_id"))
        if self.bus is not None:
            self.bus.publish(payload)
        return 202, {"accepted": True, "event_id": payload.get("event_id")}

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
        # 5s zone-state posts are the camera's primary heartbeat. Unconditional:
        # a suppressed history row is still proof the camera is alive, and
        # gating this would make every quiet zone trip R-07.
        self.store.mark_camera_seen(payload.get("camera_id"), payload.get("ts"))
        return 202, {"accepted": True, "zone_id": payload["zone_id"],
                     "recorded": history}

    # -- history / logs --
    def events_history(self, t0, t1, **f):
        return self.store.list_events(t0, t1, **f)

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
        return 200, {"saved": True, "camera_id": payload["camera_id"],
                     "count": len(payload["zones"])}

    def list_zones(self, camera_id=None):
        return self.store.list_zones(camera_id)

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
        return self.store.save_alert(alert)

    def get_alert(self, alert_id: str):
        """One alert by id, open or closed. None if unknown."""
        target = str(alert_id)
        for a in self.store.list_alerts(unacked_only=False):
            if str(a.get("alert_id")) == target:
                return a
        for a in self.store.list_alerts_history(0, time.time() + 86400, limit=100000):
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
        return 200, {"ok": True, "alert_id": alert_id, "status": action,
                     "resolved_by": who, "resolved_at": ts, "note": note}

    # -- dashboard reads --
    def zone_states(self) -> List[dict]:
        return self.store.latest_zone_states()

    def zone_state_range(self, zone_id: str, t0: float, t1: float) -> List[dict]:
        return self.store.zone_state_range(zone_id, t0, t1)
