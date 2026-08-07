"""Durable SQLite-backed store (default persistence).

A single file (data/finblade.db) — zero setup, survives restarts, and supports
date-range history queries for the Logs view. Implements the same Store
interface as InMemoryStore plus history/camera methods. Timestamps are stored as
epoch seconds (REAL); the API/UI format them as ISO for display.
"""

import json
import os
import sqlite3
import threading
import time
from typing import List, Optional

from .store import Store, _fresh_zones

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events(
  event_id TEXT PRIMARY KEY, event_type TEXT, camera_id TEXT, site_id TEXT,
  zone_id TEXT, zone_from TEXT, zone_to TEXT, person_ref TEXT,
  ts REAL, frame TEXT, payload TEXT);
CREATE INDEX IF NOT EXISTS ix_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS ix_events_type ON events(event_type);

CREATE TABLE IF NOT EXISTS zone_state_ts(
  id INTEGER PRIMARY KEY AUTOINCREMENT, zone_id TEXT, camera_id TEXT, zone_name TEXT,
  zone_type TEXT, restricted INTEGER, ts REAL, occupancy INTEGER, density REAL, capacity_pct REAL,
  peak_occupancy INTEGER, avg_occupancy REAL, trend TEXT, extra TEXT,
  inflow REAL, outflow REAL, status TEXT, site_id TEXT);
CREATE INDEX IF NOT EXISTS ix_zst_zone_ts ON zone_state_ts(zone_id, ts);
-- Covers "latest state per camera+zone", which the dashboard asks for twice a
-- second over the WebSocket. Without it SQLite cannot satisfy the grouping from
-- ix_zst_zone_ts and falls back to a temp B-tree over the whole table: measured
-- at 1.9s against 1.6M rows, versus 0.16s with this index. That cost lands in
-- the event loop and was enough to starve the socket, which connected and then
-- dropped back to REST polling within a second.
CREATE INDEX IF NOT EXISTS ix_zst_zone_cam_id ON zone_state_ts(zone_id, camera_id, id);

CREATE TABLE IF NOT EXISTS alerts(
  alert_id INTEGER PRIMARY KEY AUTOINCREMENT, rule_id TEXT, severity TEXT, message TEXT,
  zone_id TEXT, camera_id TEXT, person_ref TEXT, ts REAL, frame TEXT, kind TEXT,
  acknowledged_by TEXT, acknowledged_at REAL,
  status TEXT DEFAULT 'OPEN', note TEXT, resolved_by TEXT, resolved_at REAL,
  site_id TEXT);
CREATE INDEX IF NOT EXISTS ix_alerts_ts ON alerts(ts);

CREATE TABLE IF NOT EXISTS cameras(
  camera_id TEXT PRIMARY KEY, site_id TEXT, last_seen REAL,
  name TEXT, state TEXT, input_fps REAL, resolution TEXT, dropped_frames INTEGER,
  reconnects INTEGER, loops INTEGER, frozen INTEGER, enabled INTEGER,
  stream_url TEXT, health_ts REAL, sim_failure INTEGER DEFAULT 0, source TEXT,
  people_in_view INTEGER DEFAULT 0, people_in_zones INTEGER DEFAULT 0);

CREATE TABLE IF NOT EXISTS reports(
  report_id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, generated_at REAL,
  from_ts REAL, to_ts REAL, peak_occupancy INTEGER, total_alerts INTEGER, payload TEXT);
CREATE INDEX IF NOT EXISTS ix_reports_gen ON reports(generated_at);

-- Current state, one row per zone, overwritten in place. Separate from the
-- zone_state_ts history on purpose.
--
-- "What is happening now" and "what happened over time" are different questions
-- and were being answered from the same table. Reading the current state meant
-- SELECT ... WHERE id IN (SELECT MAX(id) ... GROUP BY zone_id, camera_id) across
-- the whole history — 1.9s against 1.6M rows until a covering index was added,
-- and that cost landed in the event loop on a query the dashboard runs twice a
-- second. Here it is a handful of rows and a full scan is free.
--
-- It also means current state survives retention: pruning zone_state_ts can no
-- longer delete the row a zone's live reading depends on.
CREATE TABLE IF NOT EXISTS zone_live(
  camera_id TEXT NOT NULL, zone_id TEXT NOT NULL, site_id TEXT,
  zone_name TEXT, zone_type TEXT, restricted INTEGER, ts REAL,
  occupancy INTEGER, density REAL, capacity_pct REAL,
  peak_occupancy INTEGER, avg_occupancy REAL, trend TEXT, extra TEXT,
  inflow REAL, outflow REAL, status TEXT,
  PRIMARY KEY (camera_id, zone_id));

-- Where the FinBlade forwarder had got to. Without this the cursor lives only
-- in the process: restart during a FinBlade outage and the backlog is skipped
-- rather than replayed, which is the exact case the design exists to survive.
CREATE TABLE IF NOT EXISTS forwarder_cursors(
  name TEXT PRIMARY KEY, ts REAL NOT NULL);

CREATE TABLE IF NOT EXISTS zones(
  camera_id TEXT, zone_id TEXT, zone_name TEXT, zone_type TEXT, restricted INTEGER,
  capacity_max INTEGER, area_sqm REAL, warning_density REAL, critical_density REAL,
  loitering_threshold_sec REAL, colour TEXT, enabled INTEGER,
  normalized_polygon TEXT, polygon TEXT, adjacency_list TEXT, updated_at REAL,
  PRIMARY KEY (camera_id, zone_id));
"""


def _row(cur) -> List[dict]:
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


class SQLiteStore(Store):
    def __init__(self, path: str = "data/finblade.db"):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()
        self._lock = threading.Lock()
        # Short cache for latest_zone_states. The WebSocket pushes a full
        # snapshot twice a second and EVERY connected dashboard runs its own
        # loop, so without this the query load multiplies by the number of
        # people watching — three tabs was enough to saturate the event loop and
        # make sockets drop. The underlying figures are a 5s aggregate, so a
        # sub-second cache cannot make them meaningfully staler.
        self._zone_cache = None          # (built_at, rows)

    def _migrate(self) -> None:
        """Add columns introduced after a DB was first created (older files)."""
        cam = {r[1] for r in self._conn.execute("PRAGMA table_info(cameras)")}
        cam_add = {
            "name": "TEXT", "state": "TEXT", "input_fps": "REAL", "resolution": "TEXT",
            "dropped_frames": "INTEGER", "reconnects": "INTEGER", "loops": "INTEGER",
            "frozen": "INTEGER", "enabled": "INTEGER", "stream_url": "TEXT",
            "health_ts": "REAL", "sim_failure": "INTEGER DEFAULT 0", "source": "TEXT",
            # Frame-wide people count, so a camera with no zones drawn
            # still reports how many people it can see.
            "people_in_view": "INTEGER DEFAULT 0",
            "people_in_zones": "INTEGER DEFAULT 0",
        }
        for col, typ in cam_add.items():
            if col not in cam:
                self._conn.execute(f"ALTER TABLE cameras ADD COLUMN {col} {typ}")
        al = {r[1] for r in self._conn.execute("PRAGMA table_info(alerts)")}
        al_add = {"status": "TEXT DEFAULT 'OPEN'", "note": "TEXT",
                  "resolved_by": "TEXT", "resolved_at": "REAL",
                  # So a record can be attributed to a site without the consumer
                  # having to join camera data. Existing rows stay NULL; the API
                  # derives the site from the camera when one is missing.
                  "site_id": "TEXT"}
        for col, typ in al_add.items():
            if col not in al:
                self._conn.execute(f"ALTER TABLE alerts ADD COLUMN {col} {typ}")
        zst = {r[1] for r in self._conn.execute("PRAGMA table_info(zone_state_ts)")}
        if "site_id" not in zst:
            self._conn.execute("ALTER TABLE zone_state_ts ADD COLUMN site_id TEXT")

        # Seed zone_live from history on the upgrade that introduces it.
        # Without this, an existing deployment shows no live zones until every
        # camera next reports — and on a box where the workers are stopped,
        # indefinitely. Runs once: after the first write the table is non-empty.
        empty = self._conn.execute(
            "SELECT COUNT(*) FROM zone_live").fetchone()[0] == 0
        if empty:
            self._conn.execute(
                "INSERT OR REPLACE INTO zone_live(camera_id,zone_id,site_id,"
                "zone_name,zone_type,restricted,ts,occupancy,density,capacity_pct,"
                "peak_occupancy,avg_occupancy,trend,extra,inflow,outflow,status) "
                "SELECT COALESCE(camera_id,''),zone_id,site_id,zone_name,zone_type,"
                "restricted,ts,occupancy,density,capacity_pct,peak_occupancy,"
                "avg_occupancy,trend,extra,inflow,outflow,status "
                "FROM zone_state_ts WHERE id IN "
                "(SELECT MAX(id) FROM zone_state_ts GROUP BY zone_id, camera_id)")

    # -- writes -------------------------------------------------------------
    def save_event(self, evt: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO events(event_id,event_type,camera_id,site_id,"
                "zone_id,zone_from,zone_to,person_ref,ts,frame,payload) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (evt.get("event_id"), evt.get("event_type"), evt.get("camera_id"),
                 evt.get("site_id"), evt.get("zone_id"), evt.get("zone_from"),
                 evt.get("zone_to"), evt.get("person_ref"), float(evt.get("timestamp", 0)),
                 evt.get("frame"), json.dumps(evt)))
            self._conn.commit()

    def save_zone_state(self, s: dict, history: bool = True) -> None:
        with self._lock:
            extra = {k: s[k] for k in ("net_flow", "inflow_5m", "outflow_5m",
                                       "inflow_15m", "outflow_15m",
                                       "capacity_max", "area_sqm") if k in s}
            if history:
                self._conn.execute(
                    "INSERT INTO zone_state_ts(zone_id,camera_id,zone_name,zone_type,restricted,ts,"
                    "occupancy,density,capacity_pct,peak_occupancy,avg_occupancy,trend,extra,inflow,outflow,status,site_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (s["zone_id"], s.get("camera_id"), s.get("zone_name"), s.get("zone_type"),
                     1 if s.get("restricted") else 0, float(s["ts"]), int(s["occupancy"]),
                     float(s["density"]), float(s["capacity_pct"]),
                     int(s.get("peak_occupancy", s["occupancy"])), float(s.get("avg_occupancy", 0)),
                     s.get("trend", "flat"), json.dumps(extra),
                     float(s.get("inflow_per_min", 0)), float(s.get("outflow_per_min", 0)),
                     s.get("status"), s.get("site_id")))
            # Same values, overwritten in place.
            #
            # ON CONFLICT ... WHERE rather than INSERT OR REPLACE, so a delayed
            # post from a slow camera arriving after a newer one cannot move the
            # live reading backwards. The old MAX(id) query took the last row
            # INSERTED, which had exactly that flaw; InMemoryStore already
            # compared timestamps, so the two backends disagreed. Both now
            # take the newest by ts.
            self._conn.execute(
                "INSERT INTO zone_live(camera_id,zone_id,site_id,"
                "zone_name,zone_type,restricted,ts,occupancy,density,capacity_pct,"
                "peak_occupancy,avg_occupancy,trend,extra,inflow,outflow,status) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(camera_id,zone_id) DO UPDATE SET "
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
            self._conn.commit()
        # A write makes the cached snapshot wrong, so drop it. Relying on the
        # TTL alone meant a post followed immediately by a read returned the
        # state from BEFORE the post — invisible while InMemoryStore, which has
        # no cache, backed every test. See the note in postgres_store.py: reads
        # outnumber writes by the number of dashboards watching, so the cache
        # still does its job and now never serves a stale reading.
        self._zone_cache = None

    def save_alert(self, a: dict) -> str:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO alerts(rule_id,severity,message,zone_id,camera_id,person_ref,"
                "ts,frame,kind,acknowledged_by,acknowledged_at,status,site_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,NULL,NULL,'OPEN',?)",
                (a.get("rule_id"), a.get("severity"), a.get("message"), a.get("zone_id"),
                 a.get("camera_id"), a.get("person_ref"), float(a.get("ts", 0)),
                 a.get("frame"), a.get("kind", "FIRE"), a.get("site_id")))
            self._conn.commit()
            return str(cur.lastrowid)

    def acknowledge_alert(self, alert_id: str, who: str, ts: float) -> bool:
        return self.update_alert(alert_id, "ACK", who, ts)

    def delete_alerts(self, scope: str = "closed"):
        """Delete alerts, returning (count, frame_refs) so snapshots can go too.

        Selects the frame refs BEFORE deleting: once the rows are gone there is
        no way to find which JPEGs they owned, and the files would sit on disk
        forever.
        """
        closed = ("RESOLVED", "DISMISSED")
        with self._lock:
            if scope == "all":
                rows = self._conn.execute(
                    "SELECT frame FROM alerts WHERE frame IS NOT NULL").fetchall()
                cur = self._conn.execute("DELETE FROM alerts")
            else:
                rows = self._conn.execute(
                    "SELECT frame FROM alerts WHERE frame IS NOT NULL "
                    "AND status IN (?,?)", closed).fetchall()
                cur = self._conn.execute(
                    "DELETE FROM alerts WHERE status IN (?,?)", closed)
            self._conn.commit()
            frames = [r["frame"] if isinstance(r, dict) or hasattr(r, "keys") else r[0]
                      for r in rows]
            return cur.rowcount, [f for f in frames if f]

    def update_alert(self, alert_id: str, status: str, who: str, ts: float,
                     note: str = None) -> bool:
        """Advance an alert through OPEN -> ACK -> RESOLVED/DISMISSED. A resolve or
        dismiss also stamps acknowledgement if it wasn't acked first."""
        try:
            aid = int(alert_id)
        except (TypeError, ValueError):
            return False
        with self._lock:
            if status == "ACK":
                cur = self._conn.execute(
                    "UPDATE alerts SET acknowledged_by=?, acknowledged_at=?, status='ACK' "
                    "WHERE alert_id=? AND COALESCE(status,'OPEN')='OPEN'",
                    (who, ts, aid))
            elif status in ("RESOLVED", "DISMISSED"):
                cur = self._conn.execute(
                    "UPDATE alerts SET status=?, resolved_by=?, resolved_at=?, note=?, "
                    "acknowledged_by=COALESCE(acknowledged_by,?), "
                    "acknowledged_at=COALESCE(acknowledged_at,?) "
                    "WHERE alert_id=? AND COALESCE(status,'OPEN') IN ('OPEN','ACK')",
                    (status, who, ts, note, who, ts, aid))
            else:
                return False
            self._conn.commit()
            return cur.rowcount > 0

    def mark_camera_seen(self, camera_id: str, ts: float, site_id: str = None) -> None:
        """Liveness only. Must NOT touch the people counts.

        It used to end with people_in_view=excluded.people_in_view — but the
        INSERT above supplies only camera_id, site_id and last_seen, so
        `excluded.people_in_view` was the column DEFAULT of 0. Every event
        ingested therefore RESET the counts to zero, and this runs on every
        event and zone-state post, far more often than the health snapshot that
        actually carries the numbers.

        Visible as: the dashboard showing 0 people with someone plainly in
        frame, or counts flickering between the real value and 0, while
        seconds_since_seen stayed fresh — because last_seen was being updated by
        the very call that was wiping the counts.
        """
        if not camera_id:
            return
        with self._lock:
            self._conn.execute(
                "INSERT INTO cameras(camera_id,site_id,last_seen) VALUES (?,?,?) "
                "ON CONFLICT(camera_id) DO UPDATE SET last_seen=excluded.last_seen, "
                "site_id=COALESCE(excluded.site_id, cameras.site_id)",
                (camera_id, site_id, ts))
            self._conn.commit()

    def record_camera_health(self, camera_id: str, health: dict, ts: float,
                             site_id: str = None) -> None:
        if not camera_id:
            return
        res = health.get("resolution")
        if isinstance(res, (list, tuple)):
            res = "x".join(str(int(v)) for v in res)
        vals = (
            camera_id, site_id, ts, ts, health.get("state"), health.get("input_fps"),
            res, health.get("dropped_frames"), health.get("reconnects"),
            health.get("loops"), 1 if health.get("frozen") else 0,
            1 if health.get("enabled", True) else 0, health.get("stream_url"),
            int(health.get("people_in_view") or 0),
            int(health.get("people_in_zones") or 0),
        )
        with self._lock:
            self._conn.execute(
                "INSERT INTO cameras(camera_id,site_id,last_seen,health_ts,state,input_fps,"
                "resolution,dropped_frames,reconnects,loops,frozen,enabled,stream_url,"
                "people_in_view,people_in_zones) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(camera_id) DO UPDATE SET last_seen=excluded.last_seen, "
                "health_ts=excluded.health_ts, state=excluded.state, "
                "input_fps=excluded.input_fps, resolution=excluded.resolution, "
                "dropped_frames=excluded.dropped_frames, reconnects=excluded.reconnects, "
                "loops=excluded.loops, frozen=excluded.frozen, enabled=excluded.enabled, "
                "stream_url=COALESCE(excluded.stream_url, cameras.stream_url), "
                "site_id=COALESCE(excluded.site_id, cameras.site_id), "
                "people_in_view=excluded.people_in_view, "
                "people_in_zones=excluded.people_in_zones",
                vals)
            self._conn.commit()

    def upsert_camera(self, camera_id: str, **fields) -> None:
        if not camera_id:
            return
        cols = [k for k in ("site_id", "name", "stream_url", "enabled", "source")
                if fields.get(k) is not None]
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO cameras(camera_id) VALUES (?)", (camera_id,))
            if cols:
                sets = ",".join(f"{c}=?" for c in cols)
                self._conn.execute(
                    f"UPDATE cameras SET {sets} WHERE camera_id=?",
                    [fields[c] for c in cols] + [camera_id])
            self._conn.commit()

    def delete_camera(self, camera_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM cameras WHERE camera_id=?", (camera_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def set_camera_sim(self, camera_id: str, on: bool) -> None:
        if not camera_id:
            return
        with self._lock:
            self._conn.execute("INSERT OR IGNORE INTO cameras(camera_id) VALUES (?)",
                               (camera_id,))
            self._conn.execute("UPDATE cameras SET sim_failure=? WHERE camera_id=?",
                               (1 if on else 0, camera_id))
            self._conn.commit()

    # -- reads --------------------------------------------------------------
    _ALERT_COLS = ("alert_id,rule_id,severity,message,zone_id,camera_id,site_id,"
                   "person_ref,ts,frame,kind,acknowledged_by,acknowledged_at,"
                   "COALESCE(status,'OPEN') AS status,note,resolved_by,resolved_at")

    def list_alerts(self, unacked_only: bool = False) -> List[dict]:
        # Active feed: fires that aren't yet resolved/dismissed (CLEAR is informational).
        q = (f"SELECT {self._ALERT_COLS} FROM alerts WHERE kind!='CLEAR' "
             "AND COALESCE(status,'OPEN') NOT IN ('RESOLVED','DISMISSED')")
        if unacked_only:
            q += " AND COALESCE(status,'OPEN')='OPEN'"
        q += " ORDER BY ts DESC LIMIT 200"
        with self._lock:
            return [self._alert_out(r) for r in _row(self._conn.execute(q))]

    # Kept well under the 5s aggregation window, so the numbers are never
    # meaningfully older than they would be anyway.
    _ZONE_CACHE_TTL = 1.0

    def latest_zone_states(self) -> List[dict]:
        cached = self._zone_cache
        if cached is not None and (time.time() - cached[0]) < self._ZONE_CACHE_TTL:
            return cached[1]
        with self._lock:
            cur = self._conn.execute(
                "SELECT zone_id,camera_id,site_id,zone_name,zone_type,restricted,ts,"
                "occupancy,density,"
                "capacity_pct,peak_occupancy,avg_occupancy,trend,extra,"
                "inflow AS inflow_per_min,outflow AS outflow_per_min,status "
                # One row per zone, so no grouping and no scan of history.
                #
                # This used to read MAX(id) ... GROUP BY zone_id, camera_id over
                # zone_state_ts. Keying on both columns was load-bearing and
                # still is, in the zone_live primary key: zone ids are unique
                # only within a camera — the editor numbers each camera's zones
                # from ZONE-01 — so keying on zone_id alone made two cameras'
                # zones overwrite each other. Measured on a six-camera site:
                # seven zones existed, five came back, and which pair collided
                # changed between requests.
                "FROM zone_live")
            out = _row(cur)
        for r in out:
            r["restricted"] = bool(r.get("restricted"))
            extra = r.pop("extra", None)
            if extra:
                try:
                    r.update(json.loads(extra))
                except Exception:
                    pass
        out = _fresh_zones(out)   # drop zones no longer reporting (removed/renamed)
        self._zone_cache = (time.time(), out)
        return out

    def zone_state_range(self, zone_id: str, t0: float, t1: float) -> List[dict]:
        with self._lock:
            return _row(self._conn.execute(
                "SELECT zone_id,ts,occupancy,density,status FROM zone_state_ts "
                "WHERE zone_id=? AND ts BETWEEN ? AND ? ORDER BY ts", (zone_id, t0, t1)))

    _STATE_ROW_COLS = ("zone_id,camera_id,zone_name,ts,occupancy,density,"
                       "capacity_pct,status")

    def zone_state_rows(self, t0: float, t1: float, camera_id=None,
                        zone_id=None) -> List[dict]:
        q = (f"SELECT {self._STATE_ROW_COLS} FROM zone_state_ts "
             "WHERE ts BETWEEN ? AND ?")
        p: list = [t0, t1]
        if camera_id:
            q += " AND camera_id=?"; p.append(camera_id)
        if zone_id:
            q += " AND zone_id=?"; p.append(zone_id)
        q += " ORDER BY ts"
        with self._lock:
            return _row(self._conn.execute(q, p))

    def zone_state_prior(self, t0: float, camera_id=None, zone_id=None) -> List[dict]:
        # MAX(id) per zone among rows at or before t0. Grouping on both columns
        # for the usual reason: zone ids repeat across cameras.
        q = (f"SELECT {self._STATE_ROW_COLS} FROM zone_state_ts WHERE id IN "
             "(SELECT MAX(id) FROM zone_state_ts WHERE ts <= ?")
        p: list = [t0]
        if camera_id:
            q += " AND camera_id=?"; p.append(camera_id)
        if zone_id:
            q += " AND zone_id=?"; p.append(zone_id)
        q += " GROUP BY zone_id, camera_id)"
        with self._lock:
            return _row(self._conn.execute(q, p))

    def list_events(self, t0: float, t1: float, camera_id=None, zone_id=None,
                    event_type=None, person_ref=None, limit: int = 500) -> List[dict]:
        q = ("SELECT event_id,event_type,camera_id,site_id,zone_id,zone_from,zone_to,"
             "person_ref,ts,frame,payload FROM events WHERE ts BETWEEN ? AND ?")
        p: list = [t0, t1]
        if camera_id:
            q += " AND camera_id=?"; p.append(camera_id)
        if zone_id:
            q += " AND (zone_id=? OR zone_from=? OR zone_to=?)"; p += [zone_id, zone_id, zone_id]
        if event_type:
            q += " AND event_type=?"; p.append(event_type)
        if person_ref:
            q += " AND person_ref=?"; p.append(person_ref)
        q += " ORDER BY ts DESC LIMIT ?"; p.append(limit)
        with self._lock:
            rows = _row(self._conn.execute(q, p))
        # Merge any extra payload fields (e.g. duration, dwell_time) that have no
        # dedicated column, without overwriting the columns we already selected.
        for r in rows:
            payload = r.pop("payload", None)
            if payload:
                try:
                    for k, v in json.loads(payload).items():
                        if r.get(k) is None:
                            r[k] = v
                except (ValueError, TypeError):
                    pass
        return rows

    def list_alerts_history(self, t0: float, t1: float, camera_id=None, rule_id=None,
                            limit: int = 500) -> List[dict]:
        q = (f"SELECT {self._ALERT_COLS} FROM alerts WHERE ts BETWEEN ? AND ?")
        p: list = [t0, t1]
        if camera_id:
            q += " AND camera_id=?"; p.append(camera_id)
        if rule_id:
            q += " AND rule_id=?"; p.append(rule_id)
        q += " ORDER BY ts DESC LIMIT ?"; p.append(limit)
        with self._lock:
            return [self._alert_out(r) for r in _row(self._conn.execute(q, p))]

    def delete_before(self, cutoff_ts: float) -> dict:
        """Drop telemetry older than cutoff_ts. See Store.delete_before.

        Deliberately does NOT run VACUUM. SQLite does not return freed pages to
        the filesystem without it, so the file will not shrink — but VACUUM
        takes an exclusive lock and rewrites the whole database, which on a
        1.1 GB file stalls every request and the event loop with it. Reclaiming
        the disk is a maintenance-window job, run by hand; keeping the row count
        bounded is what this is for.
        """
        deleted = {}
        with self._lock:
            for table in ("zone_state_ts", "events"):
                cur = self._conn.execute(
                    f"DELETE FROM {table} WHERE ts < ?", (cutoff_ts,))
                deleted[table] = cur.rowcount
            self._conn.commit()
        # The live-zone cache can hold rows that no longer exist.
        self._zone_cache = None
        return deleted

    def load_cursors(self) -> dict:
        with self._lock:
            return {r["name"]: float(r["ts"]) for r in _row(
                self._conn.execute("SELECT name, ts FROM forwarder_cursors"))}

    def save_cursors(self, cursors: dict) -> None:
        with self._lock:
            for name, ts in (cursors or {}).items():
                self._conn.execute(
                    "INSERT INTO forwarder_cursors(name,ts) VALUES (?,?) "
                    "ON CONFLICT(name) DO UPDATE SET ts=excluded.ts",
                    (name, float(ts)))
            self._conn.commit()

    def list_cameras(self) -> List[dict]:
        with self._lock:
            rows = _row(self._conn.execute(
                "SELECT camera_id,site_id,last_seen,name,state,input_fps,resolution,"
                "dropped_frames,reconnects,loops,frozen,enabled,stream_url,health_ts,"
                "sim_failure,source,people_in_view,people_in_zones "
                "FROM cameras ORDER BY camera_id"))
        for r in rows:                       # store booleans as bools, not 0/1
            for k in ("frozen", "enabled", "sim_failure"):
                if r.get(k) is not None:
                    r[k] = bool(r[k])
        return rows

    def zone_state_stats(self, t0: float, t1: float, camera_id=None,
                         zone_id=None) -> List[dict]:
        # Grouped on (camera_id, zone_id), not zone_id alone.
        #
        # Zone ids are unique only within a camera — the editor numbers every
        # camera's zones from ZONE-01 — so grouping on the id merged the lobby
        # on one camera with the loading bay on another into a single report
        # row. The averages were computed across both physical areas, the peak
        # was the higher of the two, and `MAX(camera_id)` labelled the result
        # with whichever id sorted last. Nothing in the output showed it had
        # happened. Same defect the live-state query was fixed for; this is the
        # report path.
        q = ("SELECT zone_id, camera_id, MAX(zone_name) AS zone_name, "
             "COUNT(*) AS samples, AVG(occupancy) AS avg_occupancy, "
             "MAX(occupancy) AS peak_occupancy, AVG(density) AS avg_density, "
             "MAX(density) AS peak_density, AVG(capacity_pct) AS avg_capacity_pct "
             "FROM zone_state_ts WHERE ts BETWEEN ? AND ?")
        p: list = [t0, t1]
        if camera_id:
            q += " AND camera_id=?"; p.append(camera_id)
        if zone_id:
            q += " AND zone_id=?"; p.append(zone_id)
        q += " GROUP BY camera_id, zone_id ORDER BY zone_id, camera_id"
        with self._lock:
            return _row(self._conn.execute(q, p))

    # -- reports (R-08 scheduled + on-demand) -------------------------------
    def save_report(self, report: dict) -> str:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO reports(kind,generated_at,from_ts,to_ts,peak_occupancy,"
                "total_alerts,payload) VALUES (?,?,?,?,?,?,?)",
                (report.get("kind", "scheduled"), float(report.get("generated_at", 0)),
                 float(report.get("from", 0)), float(report.get("to", 0)),
                 int(report.get("totals", {}).get("peak_total_occupancy", 0)),
                 int(report.get("totals", {}).get("total_alerts", 0)),
                 json.dumps(report)))
            self._conn.commit()
            return str(cur.lastrowid)

    def list_reports(self, limit: int = 100) -> List[dict]:
        with self._lock:
            return _row(self._conn.execute(
                "SELECT report_id,kind,generated_at,from_ts,to_ts,peak_occupancy,"
                "total_alerts FROM reports ORDER BY generated_at DESC LIMIT ?", (limit,)))

    def get_report(self, report_id: str) -> dict:
        try:
            rid = int(report_id)
        except (TypeError, ValueError):
            return None
        with self._lock:
            rows = _row(self._conn.execute(
                "SELECT payload FROM reports WHERE report_id=?", (rid,)))
        if not rows:
            return None
        try:
            return json.loads(rows[0]["payload"])
        except (ValueError, TypeError):
            return None

    # -- zones (editor-defined, persisted per camera) -----------------------
    def save_zones(self, camera_id: str, zones: List[dict]) -> None:
        import time as _t
        with self._lock:
            self._conn.execute("DELETE FROM zones WHERE camera_id=?", (camera_id,))
            for z in zones:
                self._conn.execute(
                    "INSERT OR REPLACE INTO zones(camera_id,zone_id,zone_name,zone_type,"
                    "restricted,capacity_max,area_sqm,warning_density,critical_density,"
                    "loitering_threshold_sec,colour,enabled,normalized_polygon,polygon,"
                    "adjacency_list,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (camera_id, z.get("zone_id"), z.get("zone_name"), z.get("zone_type", "MONITORED"),
                     1 if z.get("restricted") else 0, int(z.get("capacity_max", 0)),
                     float(z.get("area_sqm", 0.0)), float(z.get("warning_density", 2.0)),
                     float(z.get("critical_density", 4.0)), float(z.get("loitering_threshold_sec", 30.0)),
                     z.get("colour"), 1 if z.get("enabled", True) else 0,
                     json.dumps(z.get("normalized_polygon") or []),
                     json.dumps(z.get("polygon") or []),
                     json.dumps(z.get("adjacency_list") or []), _t.time()))
            self._conn.commit()

    def list_zones(self, camera_id: str = None) -> List[dict]:
        q = ("SELECT camera_id,zone_id,zone_name,zone_type,restricted,capacity_max,area_sqm,"
             "warning_density,critical_density,loitering_threshold_sec,colour,enabled,"
             "normalized_polygon,polygon,adjacency_list,updated_at FROM zones")
        p = []
        if camera_id is not None:
            q += " WHERE camera_id=?"; p.append(camera_id)
        q += " ORDER BY zone_id"
        with self._lock:
            rows = _row(self._conn.execute(q, p))
        for r in rows:
            r["restricted"] = bool(r["restricted"])
            r["enabled"] = bool(r["enabled"])
            for k in ("normalized_polygon", "polygon", "adjacency_list"):
                try:
                    r[k] = json.loads(r[k]) if r[k] else []
                except Exception:
                    r[k] = []
        return rows

    @staticmethod
    def _alert_out(r: dict) -> dict:
        r["alert_id"] = str(r["alert_id"])
        return r
