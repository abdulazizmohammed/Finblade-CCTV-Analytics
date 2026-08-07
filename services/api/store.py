"""Persistence layer.

Two backends behind one interface:
  * InMemoryStore   — dependency-free, used by unit tests and as a dev fallback.
  * PostgresStore   — psycopg2 against the DDL in ddl.sql (NOT run in the
                      authoring env; no server/driver here — see BLOCKERS.md B-3).

The API talks only to the Store interface, so the ingest/ack logic is fully
tested against InMemoryStore without a database.
"""

import time
from typing import Dict, List, Optional, Tuple

# Live dashboard freshness: a zone not reported within this many seconds (wall
# clock) was removed/renamed in the editor, or its camera stopped, so it is
# dropped from latest_zone_states. Wall-clock (not newest-sample-relative) so the
# last remaining zone still ages out when nothing new is being posted.
ZONE_STALE_SECONDS = 30.0


def _fresh_zones(rows: List[dict], now: Optional[float] = None) -> List[dict]:
    if not rows:
        return rows
    cutoff = (time.time() if now is None else now) - ZONE_STALE_SECONDS
    return [r for r in rows if r.get("ts", 0) >= cutoff]


class Store:
    def save_event(self, evt: dict) -> None: raise NotImplementedError
    def save_zone_state(self, state: dict) -> None: raise NotImplementedError
    def save_alert(self, alert: dict) -> str: raise NotImplementedError
    def list_alerts(self, unacked_only: bool = False) -> List[dict]: raise NotImplementedError
    def acknowledge_alert(self, alert_id: str, who: str, ts: float) -> bool: raise NotImplementedError
    def update_alert(self, alert_id: str, status: str, who: str, ts: float,
                     note: str = None) -> bool: raise NotImplementedError

    def delete_alerts(self, scope: str = "closed") -> Tuple[int, List[str]]:
        """Delete alerts. Returns (rows_deleted, frame_refs_of_deleted).

        The frame refs come back so the caller can remove the snapshot files
        too — deleting only the rows would orphan the JPEGs, which are the bulk
        of the disk usage (944 MB at last count).

        scope: "closed" = RESOLVED/DISMISSED only (safe default), "all" = every
        alert regardless of state.
        """
        return 0, []

    def latest_zone_states(self) -> List[dict]: raise NotImplementedError
    def zone_state_range(self, zone_id: str, t0: float, t1: float) -> List[dict]: raise NotImplementedError

    # History + camera-liveness (default no-ops so any backend is usable).
    def mark_camera_seen(self, camera_id: str, ts: float, site_id: str = None) -> None: pass
    def list_cameras(self) -> List[dict]: return []
    def record_camera_health(self, camera_id: str, health: dict, ts: float,
                             site_id: str = None) -> None: pass
    def upsert_camera(self, camera_id: str, **fields) -> None: pass
    def delete_camera(self, camera_id: str) -> bool: return False
    def set_camera_sim(self, camera_id: str, on: bool) -> None: pass
    def list_events(self, t0: float, t1: float, camera_id=None, zone_id=None,
                    event_type=None, person_ref=None, limit: int = 500) -> List[dict]: return []
    def list_alerts_history(self, t0: float, t1: float, camera_id=None, rule_id=None,
                            limit: int = 500) -> List[dict]: return []
    def zone_state_stats(self, t0: float, t1: float, camera_id=None,
                         zone_id=None) -> List[dict]: return []
    def save_zones(self, camera_id: str, zones: List[dict]) -> None: pass
    def list_zones(self, camera_id: str = None) -> List[dict]: return []
    def save_report(self, report: dict) -> str: return ""
    def list_reports(self, limit: int = 100) -> List[dict]: return []
    def get_report(self, report_id: str) -> dict: return None

    # Forwarder cursors. Default no-ops so a backend that does not implement
    # them simply behaves as it did before: start from now, forward what
    # happens next. Only durability is lost, never correctness.
    def load_cursors(self) -> Dict[str, float]: return {}
    def save_cursors(self, cursors: Dict[str, float]) -> None: pass

    def delete_before(self, cutoff_ts: float) -> Dict[str, int]:
        """Drop telemetry older than cutoff_ts. Returns rows deleted per table.

        TELEMETRY ONLY — zone_state_ts and events. Alerts are deliberately
        excluded: they are the operator audit trail, they are tiny next to the
        series (332 rows against 1.67M on a nine-day database), and each one may
        own a snapshot JPEG on disk. Deleting an alert row here would orphan its
        image; DELETE /api/v1/alerts exists for that and removes both.
        """
        return {}


class InMemoryStore(Store):
    def __init__(self):
        self._events: List[dict] = []
        self._zone_ts: List[dict] = []
        self._alerts: Dict[str, dict] = {}
        self._alert_seq = 0
        self._cameras: Dict[str, dict] = {}
        self._zones: Dict[str, List[dict]] = {}
        self._reports: List[dict] = []
        self._report_seq = 0

    def save_event(self, evt: dict) -> None:
        self._events.append(dict(evt))

    def save_zone_state(self, state: dict) -> None:
        self._zone_ts.append(dict(state))

    def save_alert(self, alert: dict) -> str:
        self._alert_seq += 1
        alert_id = f"al-{self._alert_seq}"
        rec = dict(alert)
        rec["alert_id"] = alert_id
        rec.setdefault("acknowledged_by", None)
        rec.setdefault("acknowledged_at", None)
        rec.setdefault("status", "OPEN")
        rec.setdefault("note", None)
        self._alerts[alert_id] = rec
        return alert_id

    def list_alerts(self, unacked_only: bool = False) -> List[dict]:
        # Active feed: fires not yet resolved/dismissed (CLEAR is informational).
        vals = [a for a in self._alerts.values()
                if a.get("kind", "FIRE") != "CLEAR"
                and a.get("status", "OPEN") not in ("RESOLVED", "DISMISSED")]
        if unacked_only:
            vals = [a for a in vals if a.get("status", "OPEN") == "OPEN"]
        return sorted(vals, key=lambda a: a.get("ts", 0), reverse=True)

    def acknowledge_alert(self, alert_id: str, who: str, ts: float) -> bool:
        return self.update_alert(alert_id, "ACK", who, ts)

    def delete_alerts(self, scope: str = "closed") -> Tuple[int, List[str]]:
        closed = ("RESOLVED", "DISMISSED")
        doomed = [aid for aid, a in self._alerts.items()
                  if scope == "all" or a.get("status", "OPEN") in closed]
        frames = [self._alerts[aid].get("frame") for aid in doomed
                  if self._alerts[aid].get("frame")]
        for aid in doomed:
            del self._alerts[aid]
        return len(doomed), frames

    def update_alert(self, alert_id: str, status: str, who: str, ts: float,
                     note: str = None) -> bool:
        a = self._alerts.get(alert_id)
        if a is None:
            return False
        cur = a.get("status", "OPEN")
        if status == "ACK":
            if cur != "OPEN":
                return False
            a["status"] = "ACK"
            a["acknowledged_by"] = who
            a["acknowledged_at"] = ts
            return True
        if status in ("RESOLVED", "DISMISSED"):
            if cur not in ("OPEN", "ACK"):
                return False
            a["status"] = status
            a["resolved_by"] = who
            a["resolved_at"] = ts
            a["note"] = note
            a.setdefault("acknowledged_by", None)
            if a.get("acknowledged_by") is None:
                a["acknowledged_by"] = who
                a["acknowledged_at"] = ts
            return True
        return False

    def latest_zone_states(self) -> List[dict]:
        # Keyed on (camera_id, zone_id), NOT zone_id alone. Zone ids are only
        # unique within a camera — the editor numbers each camera's zones from
        # ZONE-01 — so keying on the id alone makes two cameras' zones overwrite
        # each other and only the last writer survives. Site occupancy then
        # silently omits whole zones, and which ones it omits changes from one
        # request to the next.
        latest: Dict[Tuple[str, str], dict] = {}
        for s in self._zone_ts:
            key = (s.get("camera_id"), s["zone_id"])
            if key not in latest or s["ts"] >= latest[key]["ts"]:
                latest[key] = s
        return _fresh_zones(list(latest.values()))

    def zone_state_range(self, zone_id: str, t0: float, t1: float) -> List[dict]:
        return [s for s in self._zone_ts
                if s["zone_id"] == zone_id and t0 <= s["ts"] <= t1]

    def delete_before(self, cutoff_ts: float) -> Dict[str, int]:
        before_states, before_events = len(self._zone_ts), len(self._events)
        self._zone_ts = [s for s in self._zone_ts
                         if float(s.get("ts") or 0) >= cutoff_ts]
        self._events = [e for e in self._events
                        if float(e.get("ts") or e.get("timestamp") or 0) >= cutoff_ts]
        return {"zone_state_ts": before_states - len(self._zone_ts),
                "events": before_events - len(self._events)}

    def event_count(self) -> int:
        return len(self._events)

    def mark_camera_seen(self, camera_id: str, ts: float, site_id: str = None) -> None:
        if not camera_id:
            return
        c = self._cameras.setdefault(camera_id, {"camera_id": camera_id, "site_id": site_id})
        c["last_seen"] = ts
        if site_id:
            c["site_id"] = site_id

    def list_cameras(self) -> List[dict]:
        return [dict(c) for c in self._cameras.values()]

    _HEALTH_FIELDS = ("state", "input_fps", "resolution", "dropped_frames",
                      "reconnects", "frozen", "enabled", "stream_url", "loops")

    def record_camera_health(self, camera_id: str, health: dict, ts: float,
                             site_id: str = None) -> None:
        if not camera_id:
            return
        c = self._cameras.setdefault(camera_id, {"camera_id": camera_id, "site_id": site_id})
        c["last_seen"] = ts
        c["health_ts"] = ts
        if site_id:
            c["site_id"] = site_id
        for k in self._HEALTH_FIELDS:
            if k in health:
                c[k] = health[k]

    def upsert_camera(self, camera_id: str, **fields) -> None:
        if not camera_id:
            return
        c = self._cameras.setdefault(camera_id, {"camera_id": camera_id})
        for k, v in fields.items():
            if v is not None:
                c[k] = v

    def delete_camera(self, camera_id: str) -> bool:
        return self._cameras.pop(camera_id, None) is not None

    def set_camera_sim(self, camera_id: str, on: bool) -> None:
        c = self._cameras.setdefault(camera_id, {"camera_id": camera_id})
        c["sim_failure"] = bool(on)

    def list_events(self, t0, t1, camera_id=None, zone_id=None, event_type=None,
                    person_ref=None, limit=500):
        out = []
        for e in self._events:
            ts = e.get("timestamp", 0)
            if not (t0 <= ts <= t1):
                continue
            if camera_id and e.get("camera_id") != camera_id:
                continue
            if event_type and e.get("event_type") != event_type:
                continue
            if person_ref and e.get("person_ref") != person_ref:
                continue
            if zone_id and zone_id not in (e.get("zone_id"), e.get("zone_from"), e.get("zone_to")):
                continue
            out.append(e)
        out.sort(key=lambda e: e.get("timestamp", 0), reverse=True)
        return out[:limit]

    def list_alerts_history(self, t0, t1, camera_id=None, rule_id=None, limit=500):
        out = []
        for a in self._alerts.values():
            ts = a.get("ts", 0)
            if not (t0 <= ts <= t1):
                continue
            if camera_id and a.get("camera_id") != camera_id:
                continue
            if rule_id and a.get("rule_id") != rule_id:
                continue
            out.append(a)
        out.sort(key=lambda a: a.get("ts", 0), reverse=True)
        return out[:limit]

    def save_report(self, report: dict) -> str:
        self._report_seq += 1
        rid = str(self._report_seq)
        rec = dict(report)
        rec["report_id"] = rid
        self._reports.append(rec)
        return rid

    def list_reports(self, limit: int = 100) -> List[dict]:
        out = sorted(self._reports, key=lambda r: r.get("generated_at", 0), reverse=True)
        keep = ("report_id", "kind", "generated_at", "from", "to")
        rows = []
        for r in out[:limit]:
            row = {k: r.get(k) for k in keep}
            row["from_ts"], row["to_ts"] = r.get("from"), r.get("to")
            row["peak_occupancy"] = r.get("totals", {}).get("peak_total_occupancy", 0)
            row["total_alerts"] = r.get("totals", {}).get("total_alerts", 0)
            rows.append(row)
        return rows

    def get_report(self, report_id: str) -> dict:
        return next((r for r in self._reports if r.get("report_id") == report_id), None)

    def save_zones(self, camera_id, zones):
        # Replace the whole zone set for a camera (the editor saves all at once).
        self._zones[camera_id] = [dict(z, camera_id=camera_id) for z in zones]

    def list_zones(self, camera_id=None):
        if camera_id is not None:
            return [dict(z) for z in self._zones.get(camera_id, [])]
        out = []
        for zs in self._zones.values():
            out.extend(dict(z) for z in zs)
        return out

    def zone_state_stats(self, t0, t1, camera_id=None, zone_id=None):
        groups = {}
        for s in self._zone_ts:
            ts = s.get("ts", 0)
            if not (t0 <= ts <= t1):
                continue
            if camera_id and s.get("camera_id") != camera_id:
                continue
            if zone_id and s.get("zone_id") != zone_id:
                continue
            groups.setdefault(s["zone_id"], []).append(s)
        out = []
        for zid, rows in sorted(groups.items()):
            occ = [r.get("occupancy", 0) for r in rows]
            den = [r.get("density", 0.0) for r in rows]
            cap = [r.get("capacity_pct", 0.0) for r in rows]
            out.append({
                "zone_id": zid,
                "zone_name": rows[-1].get("zone_name"),
                "camera_id": rows[-1].get("camera_id"),
                "samples": len(rows),
                "avg_occupancy": sum(occ) / len(occ),
                "peak_occupancy": max(occ),
                "avg_density": sum(den) / len(den),
                "peak_density": max(den),
                "avg_capacity_pct": sum(cap) / len(cap),
            })
        return out


class PostgresStore(Store):  # pragma: no cover - requires a live DB (BLOCKERS.md B-3)
    """psycopg2-backed store. Untested here: no Postgres server/driver in the
    authoring env. Schema is ddl.sql; DSN via env DATABASE_URL."""

    def __init__(self, dsn: str):
        import psycopg2  # imported lazily so the module loads without the driver
        self._conn = psycopg2.connect(dsn)
        self._conn.autocommit = True

    def save_event(self, evt: dict) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO zone_events(event_id, event_type, camera_id, site_id, "
                "zone_from, zone_to, person_ref, ts, payload) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,to_timestamp(%s),%s)",
                (evt["event_id"], evt["event_type"], evt["camera_id"], evt["site_id"],
                 evt.get("zone_from"), evt.get("zone_to"), evt.get("person_ref"),
                 evt["timestamp"], _json(evt)),
            )

    def save_zone_state(self, state: dict) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO zone_state_ts(zone_id, camera_id, ts, occupancy, density, "
                "capacity_pct, inflow_per_min, outflow_per_min, status) "
                "VALUES (%s,%s,to_timestamp(%s),%s,%s,%s,%s,%s,%s)",
                (state["zone_id"], state["camera_id"], state["ts"], state["occupancy"],
                 state["density"], state["capacity_pct"], state["inflow_per_min"],
                 state["outflow_per_min"], state["status"]),
            )

    def save_alert(self, alert: dict) -> str:
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO alerts(rule_id, severity, message, zone_id, camera_id, "
                "person_ref, ts) VALUES (%s,%s,%s,%s,%s,%s,to_timestamp(%s)) RETURNING alert_id",
                (alert["rule_id"], alert["severity"], alert["message"], alert.get("zone_id"),
                 alert.get("camera_id"), alert.get("person_ref"), alert["ts"]),
            )
            return str(cur.fetchone()[0])

    def acknowledge_alert(self, alert_id: str, who: str, ts: float) -> bool:
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE alerts SET acknowledged_by=%s, acknowledged_at=to_timestamp(%s) "
                "WHERE alert_id=%s AND acknowledged_by IS NULL",
                (who, ts, alert_id))
            return cur.rowcount > 0

    def list_alerts(self, unacked_only: bool = False) -> List[dict]:
        q = "SELECT alert_id, rule_id, severity, message, zone_id, camera_id, person_ref, " \
            "extract(epoch from ts), acknowledged_by FROM alerts"
        if unacked_only:
            q += " WHERE acknowledged_by IS NULL"
        q += " ORDER BY ts DESC"
        with self._conn.cursor() as cur:
            cur.execute(q)
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def latest_zone_states(self) -> List[dict]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT ON (zone_id) zone_id, occupancy, density, capacity_pct, "
                "inflow_per_min, outflow_per_min, status, extract(epoch from ts) as ts "
                "FROM zone_state_ts ORDER BY zone_id, ts DESC")
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def zone_state_range(self, zone_id: str, t0: float, t1: float) -> List[dict]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT zone_id, occupancy, density, extract(epoch from ts) as ts "
                "FROM zone_state_ts WHERE zone_id=%s AND ts BETWEEN to_timestamp(%s) "
                "AND to_timestamp(%s) ORDER BY ts", (zone_id, t0, t1))
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


def _json(obj) -> str:
    import json
    return json.dumps(obj)
