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
from typing import List, Optional

from .store import Store

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
  inflow REAL, outflow REAL, status TEXT);
CREATE INDEX IF NOT EXISTS ix_zst_zone_ts ON zone_state_ts(zone_id, ts);

CREATE TABLE IF NOT EXISTS alerts(
  alert_id INTEGER PRIMARY KEY AUTOINCREMENT, rule_id TEXT, severity TEXT, message TEXT,
  zone_id TEXT, camera_id TEXT, person_ref TEXT, ts REAL, frame TEXT, kind TEXT,
  acknowledged_by TEXT, acknowledged_at REAL,
  status TEXT DEFAULT 'OPEN', note TEXT, resolved_by TEXT, resolved_at REAL);
CREATE INDEX IF NOT EXISTS ix_alerts_ts ON alerts(ts);

CREATE TABLE IF NOT EXISTS cameras(
  camera_id TEXT PRIMARY KEY, site_id TEXT, last_seen REAL,
  name TEXT, state TEXT, input_fps REAL, resolution TEXT, dropped_frames INTEGER,
  reconnects INTEGER, loops INTEGER, frozen INTEGER, enabled INTEGER,
  stream_url TEXT, health_ts REAL, sim_failure INTEGER DEFAULT 0);

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

    def _migrate(self) -> None:
        """Add columns introduced after a DB was first created (older files)."""
        cam = {r[1] for r in self._conn.execute("PRAGMA table_info(cameras)")}
        cam_add = {
            "name": "TEXT", "state": "TEXT", "input_fps": "REAL", "resolution": "TEXT",
            "dropped_frames": "INTEGER", "reconnects": "INTEGER", "loops": "INTEGER",
            "frozen": "INTEGER", "enabled": "INTEGER", "stream_url": "TEXT",
            "health_ts": "REAL", "sim_failure": "INTEGER DEFAULT 0",
        }
        for col, typ in cam_add.items():
            if col not in cam:
                self._conn.execute(f"ALTER TABLE cameras ADD COLUMN {col} {typ}")
        al = {r[1] for r in self._conn.execute("PRAGMA table_info(alerts)")}
        al_add = {"status": "TEXT DEFAULT 'OPEN'", "note": "TEXT",
                  "resolved_by": "TEXT", "resolved_at": "REAL"}
        for col, typ in al_add.items():
            if col not in al:
                self._conn.execute(f"ALTER TABLE alerts ADD COLUMN {col} {typ}")

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

    def save_zone_state(self, s: dict) -> None:
        with self._lock:
            extra = {k: s[k] for k in ("net_flow", "inflow_5m", "outflow_5m",
                                       "inflow_15m", "outflow_15m") if k in s}
            self._conn.execute(
                "INSERT INTO zone_state_ts(zone_id,camera_id,zone_name,zone_type,restricted,ts,"
                "occupancy,density,capacity_pct,peak_occupancy,avg_occupancy,trend,extra,inflow,outflow,status) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (s["zone_id"], s.get("camera_id"), s.get("zone_name"), s.get("zone_type"),
                 1 if s.get("restricted") else 0, float(s["ts"]), int(s["occupancy"]),
                 float(s["density"]), float(s["capacity_pct"]),
                 int(s.get("peak_occupancy", s["occupancy"])), float(s.get("avg_occupancy", 0)),
                 s.get("trend", "flat"), json.dumps(extra),
                 float(s.get("inflow_per_min", 0)), float(s.get("outflow_per_min", 0)),
                 s.get("status")))
            self._conn.commit()

    def save_alert(self, a: dict) -> str:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO alerts(rule_id,severity,message,zone_id,camera_id,person_ref,"
                "ts,frame,kind,acknowledged_by,acknowledged_at,status) "
                "VALUES (?,?,?,?,?,?,?,?,?,NULL,NULL,'OPEN')",
                (a.get("rule_id"), a.get("severity"), a.get("message"), a.get("zone_id"),
                 a.get("camera_id"), a.get("person_ref"), float(a.get("ts", 0)),
                 a.get("frame"), a.get("kind", "FIRE")))
            self._conn.commit()
            return str(cur.lastrowid)

    def acknowledge_alert(self, alert_id: str, who: str, ts: float) -> bool:
        return self.update_alert(alert_id, "ACK", who, ts)

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
        )
        with self._lock:
            self._conn.execute(
                "INSERT INTO cameras(camera_id,site_id,last_seen,health_ts,state,input_fps,"
                "resolution,dropped_frames,reconnects,loops,frozen,enabled,stream_url) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(camera_id) DO UPDATE SET last_seen=excluded.last_seen, "
                "health_ts=excluded.health_ts, state=excluded.state, "
                "input_fps=excluded.input_fps, resolution=excluded.resolution, "
                "dropped_frames=excluded.dropped_frames, reconnects=excluded.reconnects, "
                "loops=excluded.loops, frozen=excluded.frozen, enabled=excluded.enabled, "
                "stream_url=COALESCE(excluded.stream_url, cameras.stream_url), "
                "site_id=COALESCE(excluded.site_id, cameras.site_id)",
                vals)
            self._conn.commit()

    def upsert_camera(self, camera_id: str, **fields) -> None:
        if not camera_id:
            return
        cols = [k for k in ("site_id", "name", "stream_url", "enabled")
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
    _ALERT_COLS = ("alert_id,rule_id,severity,message,zone_id,camera_id,person_ref,ts,"
                   "frame,kind,acknowledged_by,acknowledged_at,"
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

    def latest_zone_states(self) -> List[dict]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT zone_id,camera_id,zone_name,zone_type,restricted,ts,occupancy,density,"
                "capacity_pct,peak_occupancy,avg_occupancy,trend,extra,"
                "inflow AS inflow_per_min,outflow AS outflow_per_min,status "
                "FROM zone_state_ts WHERE id IN "
                "(SELECT MAX(id) FROM zone_state_ts GROUP BY zone_id)")
            out = _row(cur)
        for r in out:
            r["restricted"] = bool(r.get("restricted"))
            extra = r.pop("extra", None)
            if extra:
                try:
                    r.update(json.loads(extra))
                except Exception:
                    pass
        return out

    def zone_state_range(self, zone_id: str, t0: float, t1: float) -> List[dict]:
        with self._lock:
            return _row(self._conn.execute(
                "SELECT zone_id,ts,occupancy,density,status FROM zone_state_ts "
                "WHERE zone_id=? AND ts BETWEEN ? AND ? ORDER BY ts", (zone_id, t0, t1)))

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

    def list_cameras(self) -> List[dict]:
        with self._lock:
            rows = _row(self._conn.execute(
                "SELECT camera_id,site_id,last_seen,name,state,input_fps,resolution,"
                "dropped_frames,reconnects,loops,frozen,enabled,stream_url,health_ts,"
                "sim_failure FROM cameras ORDER BY camera_id"))
        for r in rows:                       # store booleans as bools, not 0/1
            for k in ("frozen", "enabled", "sim_failure"):
                if r.get(k) is not None:
                    r[k] = bool(r[k])
        return rows

    def zone_state_stats(self, t0: float, t1: float, camera_id=None,
                         zone_id=None) -> List[dict]:
        q = ("SELECT zone_id, MAX(zone_name) AS zone_name, MAX(camera_id) AS camera_id, "
             "COUNT(*) AS samples, AVG(occupancy) AS avg_occupancy, "
             "MAX(occupancy) AS peak_occupancy, AVG(density) AS avg_density, "
             "MAX(density) AS peak_density, AVG(capacity_pct) AS avg_capacity_pct "
             "FROM zone_state_ts WHERE ts BETWEEN ? AND ?")
        p: list = [t0, t1]
        if camera_id:
            q += " AND camera_id=?"; p.append(camera_id)
        if zone_id:
            q += " AND zone_id=?"; p.append(zone_id)
        q += " GROUP BY zone_id ORDER BY zone_id"
        with self._lock:
            return _row(self._conn.execute(q, p))

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
