"""Postgres-backed store. Same behaviour as SQLiteStore, different engine.

Written against sqlite_store.py method for method, because that file — not the
old hand-written ddl.sql — is the only authority on what this system actually
stores and how it behaves. Every non-obvious rule in there was learned from a
bug, and re-deriving them here would mean re-finding them:

  * zone_live is keyed on (camera_id, zone_id). Zone ids are unique only within
    a camera, so keying on the id alone makes two cameras' zones overwrite each
    other and site occupancy silently omits whole zones.
  * mark_camera_seen must NOT touch the people counts. It once ended with
    people_in_view=excluded.people_in_view, where `excluded` was the column
    default of 0 — so every event ingested reset the counts while keeping
    last_seen fresh.
  * The live reading only moves forward: ON CONFLICT ... WHERE excluded.ts >=
    zone_live.ts, so a delayed post from a slow camera cannot rewind it.
  * zone_state_stats groups on (camera_id, zone_id), not zone_id.
  * delete_alerts collects frame refs BEFORE deleting, or the JPEGs are
    orphaned on disk with nothing left pointing at them.

tests/test_store_conformance.py runs one suite against all three backends so
these stay true rather than being true on the day they were written.

DIFFERENCES FROM SQLITE THAT ARE DELIBERATE

A connection POOL rather than one connection behind a threading.Lock. The lock
in SQLiteStore exists because a single sqlite3 connection is not safely shared;
serialising every query through it is the concurrency ceiling this migration is
meant to lift, so copying that pattern here would move to Postgres and keep the
bottleneck.

Timestamps stay DOUBLE PRECISION epoch seconds. The application speaks epoch
end to end; converting at the storage boundary means converting back on every
read, and every conversion is a chance to lose a timezone. The analytics views
expose a real timestamptz for SQL clients, which is where it is wanted.

Schema comes from services/api/ddl_pg.sql, generated from the live SQLite
schema by scripts/gen_pg_ddl.py.
"""

import json
import os
import time
from typing import List, Optional

from .store import Store, _fresh_zones

DDL_PATH = os.path.join(os.path.dirname(__file__), "ddl_pg.sql")


def _row(cur) -> List[dict]:
    cols = [c.name for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


class PostgresStore(Store):
    # Kept well under the 5s aggregation window, as in SQLiteStore, so the
    # numbers are never meaningfully older than they would be anyway.
    _ZONE_CACHE_TTL = 1.0

    def __init__(self, dsn: str, min_size: int = 1, max_size: int = 8,
                 apply_schema: bool = True):
        from psycopg_pool import ConnectionPool

        self.dsn = dsn
        # open=True connects eagerly: a bad DSN should fail at startup, not on
        # the first request an hour later.
        self._pool = ConnectionPool(dsn, min_size=min_size, max_size=max_size,
                                    kwargs={"autocommit": True}, open=True)
        self._pool.wait(timeout=15.0)
        if apply_schema:
            self._apply_schema()
        self._zone_cache = None          # (built_at, rows)

    def close(self) -> None:
        self._pool.close()

    # -- plumbing -----------------------------------------------------------
    def _apply_schema(self) -> None:
        if not os.path.exists(DDL_PATH):
            raise RuntimeError(
                f"{DDL_PATH} missing — run scripts/gen_pg_ddl.py to generate it")
        with open(DDL_PATH) as fh:
            ddl = fh.read()
        with self._pool.connection() as conn:
            conn.execute(ddl)

    def _q(self, sql: str, params=()) -> List[dict]:
        with self._pool.connection() as conn:
            return _row(conn.execute(sql, params))

    def _x(self, sql: str, params=()) -> int:
        """Execute, returning affected rows."""
        with self._pool.connection() as conn:
            return conn.execute(sql, params).rowcount

    def _one(self, sql: str, params=()):
        with self._pool.connection() as conn:
            got = conn.execute(sql, params).fetchone()
            return got[0] if got else None

    # -- writes -------------------------------------------------------------
    def save_event(self, evt: dict) -> None:
        # ON CONFLICT DO UPDATE rather than DO NOTHING, matching SQLite's
        # INSERT OR REPLACE: a replayed event should overwrite, not be dropped.
        self._x(
            "INSERT INTO events(event_id,event_type,camera_id,site_id,"
            "zone_id,zone_from,zone_to,person_ref,ts,frame,payload) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (event_id) DO UPDATE SET "
            "event_type=excluded.event_type, camera_id=excluded.camera_id, "
            "site_id=excluded.site_id, zone_id=excluded.zone_id, "
            "zone_from=excluded.zone_from, zone_to=excluded.zone_to, "
            "person_ref=excluded.person_ref, ts=excluded.ts, "
            "frame=excluded.frame, payload=excluded.payload",
            (evt.get("event_id"), evt.get("event_type"), evt.get("camera_id"),
             evt.get("site_id"), evt.get("zone_id"), evt.get("zone_from"),
             evt.get("zone_to"), evt.get("person_ref"),
             float(evt.get("timestamp", 0)), evt.get("frame"), json.dumps(evt)))

    def save_zone_state(self, s: dict, history: bool = True) -> None:
        extra = {k: s[k] for k in ("net_flow", "inflow_5m", "outflow_5m",
                                   "inflow_15m", "outflow_15m",
                                   "capacity_max", "area_sqm") if k in s}
        with self._pool.connection() as conn:
            if history:
                conn.execute(
                    "INSERT INTO zone_state_ts(zone_id,camera_id,zone_name,zone_type,"
                    "restricted,ts,occupancy,density,capacity_pct,peak_occupancy,"
                    "avg_occupancy,trend,extra,inflow,outflow,status,site_id) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (s["zone_id"], s.get("camera_id"), s.get("zone_name"),
                     s.get("zone_type"), 1 if s.get("restricted") else 0,
                     float(s["ts"]), int(s["occupancy"]), float(s["density"]),
                     float(s["capacity_pct"]),
                     int(s.get("peak_occupancy", s["occupancy"])),
                     float(s.get("avg_occupancy", 0)), s.get("trend", "flat"),
                     json.dumps(extra), float(s.get("inflow_per_min", 0)),
                     float(s.get("outflow_per_min", 0)), s.get("status"),
                     s.get("site_id")))
            # Same values, overwritten in place. The WHERE on the DO UPDATE is
            # load-bearing: a delayed post from a slow camera arriving after a
            # newer one must not move the live reading backwards.
            conn.execute(
                "INSERT INTO zone_live(camera_id,zone_id,site_id,zone_name,zone_type,"
                "restricted,ts,occupancy,density,capacity_pct,peak_occupancy,"
                "avg_occupancy,trend,extra,inflow,outflow,status) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (camera_id,zone_id) DO UPDATE SET "
                "site_id=excluded.site_id, zone_name=excluded.zone_name, "
                "zone_type=excluded.zone_type, restricted=excluded.restricted, "
                "ts=excluded.ts, occupancy=excluded.occupancy, "
                "density=excluded.density, capacity_pct=excluded.capacity_pct, "
                "peak_occupancy=excluded.peak_occupancy, "
                "avg_occupancy=excluded.avg_occupancy, trend=excluded.trend, "
                "extra=excluded.extra, inflow=excluded.inflow, "
                "outflow=excluded.outflow, status=excluded.status "
                "WHERE excluded.ts >= zone_live.ts",
                (s.get("camera_id") or "", s["zone_id"], s.get("site_id"),
                 s.get("zone_name"), s.get("zone_type"),
                 1 if s.get("restricted") else 0, float(s["ts"]),
                 int(s["occupancy"]), float(s["density"]), float(s["capacity_pct"]),
                 int(s.get("peak_occupancy", s["occupancy"])),
                 float(s.get("avg_occupancy", 0)), s.get("trend", "flat"),
                 json.dumps(extra), float(s.get("inflow_per_min", 0)),
                 float(s.get("outflow_per_min", 0)), s.get("status")))
        # A write makes the cached snapshot wrong, so drop it.
        #
        # The TTL alone was enough while InMemoryStore — which has no cache —
        # backed every test, and it hid a real behaviour: a post followed
        # immediately by a read returned the state from before the post. Three
        # app-level tests caught it the moment the suite ran on Postgres.
        #
        # This does not undo the cache. It exists because every connected
        # dashboard polls twice a second while a camera writes once every five,
        # so reads outnumber writes by the number of people watching; dropping
        # it on write costs about one extra query per second and never serves a
        # reading older than the last post.
        self._zone_cache = None

    def save_alert(self, a: dict) -> str:
        # RETURNING, because Postgres has no lastrowid.
        return str(self._one(
            "INSERT INTO alerts(rule_id,severity,message,zone_id,camera_id,person_ref,"
            "ts,frame,kind,acknowledged_by,acknowledged_at,status,site_id) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL,NULL,'OPEN',%s) "
            "RETURNING alert_id",
            (a.get("rule_id"), a.get("severity"), a.get("message"), a.get("zone_id"),
             a.get("camera_id"), a.get("person_ref"), float(a.get("ts", 0)),
             a.get("frame"), a.get("kind", "FIRE"), a.get("site_id"))))

    def acknowledge_alert(self, alert_id: str, who: str, ts: float) -> bool:
        return self.update_alert(alert_id, "ACK", who, ts)

    def delete_alerts(self, scope: str = "closed"):
        """Delete alerts, returning (count, frame_refs) so snapshots can go too.

        Frame refs are selected BEFORE the delete: once the rows are gone there
        is nothing left pointing at the JPEGs and they sit on disk forever.
        """
        closed = ("RESOLVED", "DISMISSED")
        with self._pool.connection() as conn:
            if scope == "all":
                rows = conn.execute(
                    "SELECT frame FROM alerts WHERE frame IS NOT NULL").fetchall()
                cur = conn.execute("DELETE FROM alerts")
            else:
                rows = conn.execute(
                    "SELECT frame FROM alerts WHERE frame IS NOT NULL "
                    "AND status = ANY(%s)", (list(closed),)).fetchall()
                cur = conn.execute("DELETE FROM alerts WHERE status = ANY(%s)",
                                   (list(closed),))
            return cur.rowcount, [r[0] for r in rows if r[0]]

    def update_alert(self, alert_id: str, status: str, who: str, ts: float,
                     note: str = None) -> bool:
        try:
            aid = int(alert_id)
        except (TypeError, ValueError):
            return False
        if status == "ACK":
            return self._x(
                "UPDATE alerts SET acknowledged_by=%s, acknowledged_at=%s, status='ACK' "
                "WHERE alert_id=%s AND COALESCE(status,'OPEN')='OPEN'",
                (who, ts, aid)) > 0
        if status in ("RESOLVED", "DISMISSED"):
            return self._x(
                "UPDATE alerts SET status=%s, resolved_by=%s, resolved_at=%s, note=%s, "
                "acknowledged_by=COALESCE(acknowledged_by,%s), "
                "acknowledged_at=COALESCE(acknowledged_at,%s) "
                "WHERE alert_id=%s AND COALESCE(status,'OPEN') IN ('OPEN','ACK')",
                (status, who, ts, note, who, ts, aid)) > 0
        return False

    def mark_camera_seen(self, camera_id: str, ts: float, site_id: str = None) -> None:
        """Liveness only. Must NOT touch the people counts.

        In SQLite this once ended with people_in_view=excluded.people_in_view,
        where `excluded` was the column default of 0 because the INSERT supplies
        only camera_id, site_id and last_seen. Every event ingested therefore
        reset the counts, and this runs far more often than the health snapshot
        that carries the real numbers. Symptom: the dashboard reading 0 people
        with someone plainly in frame, while seconds_since_seen stayed fresh.
        """
        if not camera_id:
            return
        self._x(
            "INSERT INTO cameras(camera_id,site_id,last_seen) VALUES (%s,%s,%s) "
            "ON CONFLICT (camera_id) DO UPDATE SET last_seen=excluded.last_seen, "
            "site_id=COALESCE(excluded.site_id, cameras.site_id)",
            (camera_id, site_id, ts))

    def record_camera_health(self, camera_id: str, health: dict, ts: float,
                             site_id: str = None) -> None:
        if not camera_id:
            return
        res = health.get("resolution")
        if isinstance(res, (list, tuple)):
            res = "x".join(str(int(v)) for v in res)
        self._x(
            "INSERT INTO cameras(camera_id,site_id,last_seen,health_ts,state,input_fps,"
            "resolution,dropped_frames,reconnects,loops,frozen,enabled,stream_url,"
            "people_in_view,people_in_zones) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (camera_id) DO UPDATE SET last_seen=excluded.last_seen, "
            "health_ts=excluded.health_ts, state=excluded.state, "
            "input_fps=excluded.input_fps, resolution=excluded.resolution, "
            "dropped_frames=excluded.dropped_frames, reconnects=excluded.reconnects, "
            "loops=excluded.loops, frozen=excluded.frozen, enabled=excluded.enabled, "
            "stream_url=COALESCE(excluded.stream_url, cameras.stream_url), "
            "site_id=COALESCE(excluded.site_id, cameras.site_id), "
            "people_in_view=excluded.people_in_view, "
            "people_in_zones=excluded.people_in_zones",
            (camera_id, site_id, ts, ts, health.get("state"), health.get("input_fps"),
             res, health.get("dropped_frames"), health.get("reconnects"),
             health.get("loops"), 1 if health.get("frozen") else 0,
             1 if health.get("enabled", True) else 0, health.get("stream_url"),
             int(health.get("people_in_view") or 0),
             int(health.get("people_in_zones") or 0)))

    def upsert_camera(self, camera_id: str, **fields) -> None:
        if not camera_id:
            return
        cols = [k for k in ("site_id", "name", "stream_url", "enabled", "source")
                if fields.get(k) is not None]
        with self._pool.connection() as conn:
            conn.execute("INSERT INTO cameras(camera_id) VALUES (%s) "
                         "ON CONFLICT (camera_id) DO NOTHING", (camera_id,))
            if cols:
                sets = ",".join(f"{c}=%s" for c in cols)
                conn.execute(f"UPDATE cameras SET {sets} WHERE camera_id=%s",
                             [fields[c] for c in cols] + [camera_id])

    def delete_camera(self, camera_id: str) -> bool:
        return self._x("DELETE FROM cameras WHERE camera_id=%s", (camera_id,)) > 0

    def set_camera_sim(self, camera_id: str, on: bool) -> None:
        if not camera_id:
            return
        with self._pool.connection() as conn:
            conn.execute("INSERT INTO cameras(camera_id) VALUES (%s) "
                         "ON CONFLICT (camera_id) DO NOTHING", (camera_id,))
            conn.execute("UPDATE cameras SET sim_failure=%s WHERE camera_id=%s",
                         (1 if on else 0, camera_id))

    # -- reads --------------------------------------------------------------
    _ALERT_COLS = ("alert_id,rule_id,severity,message,zone_id,camera_id,site_id,"
                   "person_ref,ts,frame,kind,acknowledged_by,acknowledged_at,"
                   "COALESCE(status,'OPEN') AS status,note,resolved_by,resolved_at")

    def list_alerts(self, unacked_only: bool = False) -> List[dict]:
        q = (f"SELECT {self._ALERT_COLS} FROM alerts WHERE kind!='CLEAR' "
             "AND COALESCE(status,'OPEN') NOT IN ('RESOLVED','DISMISSED')")
        if unacked_only:
            q += " AND COALESCE(status,'OPEN')='OPEN'"
        q += " ORDER BY ts DESC LIMIT 200"
        return [self._alert_out(r) for r in self._q(q)]

    def latest_zone_states(self) -> List[dict]:
        cached = self._zone_cache
        if cached is not None and (time.time() - cached[0]) < self._ZONE_CACHE_TTL:
            return cached[1]
        out = self._q(
            "SELECT zone_id,camera_id,site_id,zone_name,zone_type,restricted,ts,"
            "occupancy,density,capacity_pct,peak_occupancy,avg_occupancy,trend,extra,"
            "inflow AS inflow_per_min,outflow AS outflow_per_min,status "
            "FROM zone_live")
        for r in out:
            r["restricted"] = bool(r.get("restricted"))
            extra = r.pop("extra", None)
            if extra:
                try:
                    r.update(json.loads(extra))
                except Exception:                       # noqa: BLE001
                    pass
        out = _fresh_zones(out)
        self._zone_cache = (time.time(), out)
        return out

    def zone_state_range(self, zone_id: str, t0: float, t1: float) -> List[dict]:
        return self._q(
            "SELECT zone_id,ts,occupancy,density,status FROM zone_state_ts "
            "WHERE zone_id=%s AND ts BETWEEN %s AND %s ORDER BY ts",
            (zone_id, t0, t1))

    _STATE_ROW_COLS = ("zone_id,camera_id,zone_name,ts,occupancy,density,"
                       "capacity_pct,status")

    def zone_state_rows(self, t0: float, t1: float, camera_id=None,
                        zone_id=None) -> List[dict]:
        q = (f"SELECT {self._STATE_ROW_COLS} FROM zone_state_ts "
             "WHERE ts BETWEEN %s AND %s")
        p: list = [t0, t1]
        if camera_id:
            q += " AND camera_id=%s"; p.append(camera_id)
        if zone_id:
            q += " AND zone_id=%s"; p.append(zone_id)
        q += " ORDER BY ts"
        return self._q(q, p)

    def zone_state_prior(self, t0: float, camera_id=None, zone_id=None) -> List[dict]:
        q = (f"SELECT {self._STATE_ROW_COLS} FROM zone_state_ts WHERE id IN "
             "(SELECT MAX(id) FROM zone_state_ts WHERE ts <= %s")
        p: list = [t0]
        if camera_id:
            q += " AND camera_id=%s"; p.append(camera_id)
        if zone_id:
            q += " AND zone_id=%s"; p.append(zone_id)
        q += " GROUP BY zone_id, camera_id)"
        return self._q(q, p)

    def list_events(self, t0: float, t1: float, camera_id=None, zone_id=None,
                    event_type=None, person_ref=None, limit: int = 500) -> List[dict]:
        q = ("SELECT event_id,event_type,camera_id,site_id,zone_id,zone_from,zone_to,"
             "person_ref,ts,frame,payload FROM events WHERE ts BETWEEN %s AND %s")
        p: list = [t0, t1]
        if camera_id:
            q += " AND camera_id=%s"; p.append(camera_id)
        if zone_id:
            q += " AND (zone_id=%s OR zone_from=%s OR zone_to=%s)"
            p += [zone_id, zone_id, zone_id]
        if event_type:
            q += " AND event_type=%s"; p.append(event_type)
        if person_ref:
            q += " AND person_ref=%s"; p.append(person_ref)
        q += " ORDER BY ts DESC LIMIT %s"; p.append(limit)
        rows = self._q(q, p)
        # Merge payload fields with no dedicated column, without overwriting
        # the columns already selected.
        for r in rows:
            payload = r.pop("payload", None)
            if payload:
                try:
                    parsed = json.loads(payload) if isinstance(payload, str) else payload
                    for k, v in parsed.items():
                        if r.get(k) is None:
                            r[k] = v
                except (ValueError, TypeError, AttributeError):
                    pass
        return rows

    def list_alerts_history(self, t0: float, t1: float, camera_id=None, rule_id=None,
                            limit: int = 500) -> List[dict]:
        q = f"SELECT {self._ALERT_COLS} FROM alerts WHERE ts BETWEEN %s AND %s"
        p: list = [t0, t1]
        if camera_id:
            q += " AND camera_id=%s"; p.append(camera_id)
        if rule_id:
            q += " AND rule_id=%s"; p.append(rule_id)
        q += " ORDER BY ts DESC LIMIT %s"; p.append(limit)
        return [self._alert_out(r) for r in self._q(q, p)]

    def delete_before(self, cutoff_ts: float) -> dict:
        """Drop telemetry older than cutoff_ts. See Store.delete_before.

        No VACUUM FULL, for the same reason SQLite does not VACUUM: it takes an
        exclusive lock and rewrites the table. Postgres autovacuum reclaims the
        space for reuse on its own, which is what matters — keeping the row
        count bounded, not shrinking the file.
        """
        deleted = {}
        with self._pool.connection() as conn:
            for table in ("zone_state_ts", "events"):
                deleted[table] = conn.execute(
                    f"DELETE FROM {table} WHERE ts < %s", (cutoff_ts,)).rowcount
        self._zone_cache = None
        return deleted

    def load_cursors(self) -> dict:
        return {r["name"]: float(r["ts"])
                for r in self._q("SELECT name, ts FROM forwarder_cursors")}

    def save_cursors(self, cursors: dict) -> None:
        with self._pool.connection() as conn:
            for name, ts in (cursors or {}).items():
                conn.execute(
                    "INSERT INTO forwarder_cursors(name,ts) VALUES (%s,%s) "
                    "ON CONFLICT (name) DO UPDATE SET ts=excluded.ts",
                    (name, float(ts)))

    def list_cameras(self) -> List[dict]:
        rows = self._q(
            "SELECT camera_id,site_id,last_seen,name,state,input_fps,resolution,"
            "dropped_frames,reconnects,loops,frozen,enabled,stream_url,health_ts,"
            "sim_failure,source,people_in_view,people_in_zones "
            "FROM cameras ORDER BY camera_id")
        for r in rows:
            for k in ("frozen", "enabled", "sim_failure"):
                if r.get(k) is not None:
                    r[k] = bool(r[k])
        return rows

    def zone_state_stats(self, t0: float, t1: float, camera_id=None,
                         zone_id=None) -> List[dict]:
        # (camera_id, zone_id), not zone_id alone — grouping on the id merges
        # two cameras' unrelated zones into one row and averages across both
        # physical areas.
        q = ("SELECT zone_id, camera_id, MAX(zone_name) AS zone_name, "
             "COUNT(*) AS samples, AVG(occupancy) AS avg_occupancy, "
             "MAX(occupancy) AS peak_occupancy, AVG(density) AS avg_density, "
             "MAX(density) AS peak_density, AVG(capacity_pct) AS avg_capacity_pct "
             "FROM zone_state_ts WHERE ts BETWEEN %s AND %s")
        p: list = [t0, t1]
        if camera_id:
            q += " AND camera_id=%s"; p.append(camera_id)
        if zone_id:
            q += " AND zone_id=%s"; p.append(zone_id)
        q += " GROUP BY camera_id, zone_id ORDER BY zone_id, camera_id"
        rows = self._q(q, p)
        # AVG() returns Decimal in Postgres and float in SQLite. Callers do
        # arithmetic on these and json.dumps them; a Decimal raises on encode
        # and compares unequal to the float the other backends return.
        for r in rows:
            for k in ("avg_occupancy", "avg_density", "avg_capacity_pct",
                      "peak_density"):
                if r.get(k) is not None:
                    r[k] = float(r[k])
        return rows

    # -- reports ------------------------------------------------------------
    def save_report(self, report: dict) -> str:
        return str(self._one(
            "INSERT INTO reports(kind,generated_at,from_ts,to_ts,peak_occupancy,"
            "total_alerts,payload) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING report_id",
            (report.get("kind", "scheduled"), float(report.get("generated_at", 0)),
             float(report.get("from", 0)), float(report.get("to", 0)),
             int(report.get("totals", {}).get("peak_total_occupancy", 0)),
             int(report.get("totals", {}).get("total_alerts", 0)),
             json.dumps(report))))

    def list_reports(self, limit: int = 100) -> List[dict]:
        return self._q(
            "SELECT report_id,kind,generated_at,from_ts,to_ts,peak_occupancy,"
            "total_alerts FROM reports ORDER BY generated_at DESC LIMIT %s", (limit,))

    def get_report(self, report_id: str) -> Optional[dict]:
        try:
            rid = int(report_id)
        except (TypeError, ValueError):
            return None
        rows = self._q("SELECT payload FROM reports WHERE report_id=%s", (rid,))
        if not rows:
            return None
        payload = rows[0]["payload"]
        try:
            return json.loads(payload) if isinstance(payload, str) else payload
        except (ValueError, TypeError):
            return None

    # -- zones --------------------------------------------------------------
    def save_zones(self, camera_id: str, zones: List[dict]) -> None:
        with self._pool.connection() as conn:
            conn.execute("DELETE FROM zones WHERE camera_id=%s", (camera_id,))
            for z in zones:
                conn.execute(
                    "INSERT INTO zones(camera_id,zone_id,zone_name,zone_type,"
                    "restricted,capacity_max,area_sqm,warning_density,critical_density,"
                    "loitering_threshold_sec,colour,enabled,normalized_polygon,polygon,"
                    "adjacency_list,updated_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (camera_id,zone_id) DO UPDATE SET "
                    "zone_name=excluded.zone_name, zone_type=excluded.zone_type, "
                    "restricted=excluded.restricted, capacity_max=excluded.capacity_max, "
                    "area_sqm=excluded.area_sqm, warning_density=excluded.warning_density, "
                    "critical_density=excluded.critical_density, "
                    "loitering_threshold_sec=excluded.loitering_threshold_sec, "
                    "colour=excluded.colour, enabled=excluded.enabled, "
                    "normalized_polygon=excluded.normalized_polygon, "
                    "polygon=excluded.polygon, adjacency_list=excluded.adjacency_list, "
                    "updated_at=excluded.updated_at",
                    (camera_id, z.get("zone_id"), z.get("zone_name"),
                     z.get("zone_type", "MONITORED"),
                     1 if z.get("restricted") else 0, int(z.get("capacity_max", 0)),
                     float(z.get("area_sqm", 0.0)), float(z.get("warning_density", 2.0)),
                     float(z.get("critical_density", 4.0)),
                     float(z.get("loitering_threshold_sec", 30.0)),
                     z.get("colour"), 1 if z.get("enabled", True) else 0,
                     json.dumps(z.get("normalized_polygon") or []),
                     json.dumps(z.get("polygon") or []),
                     json.dumps(z.get("adjacency_list") or []), time.time()))

    def list_zones(self, camera_id: str = None) -> List[dict]:
        q = ("SELECT camera_id,zone_id,zone_name,zone_type,restricted,capacity_max,"
             "area_sqm,warning_density,critical_density,loitering_threshold_sec,"
             "colour,enabled,normalized_polygon,polygon,adjacency_list,updated_at "
             "FROM zones")
        p = []
        if camera_id is not None:
            q += " WHERE camera_id=%s"; p.append(camera_id)
        q += " ORDER BY zone_id"
        rows = self._q(q, p)
        for r in rows:
            r["restricted"] = bool(r["restricted"])
            r["enabled"] = bool(r["enabled"])
            for k in ("normalized_polygon", "polygon", "adjacency_list"):
                try:
                    r[k] = json.loads(r[k]) if r[k] else []
                except Exception:                       # noqa: BLE001
                    r[k] = []
        return rows

    @staticmethod
    def _alert_out(r: dict) -> dict:
        r["alert_id"] = str(r["alert_id"])
        return r
