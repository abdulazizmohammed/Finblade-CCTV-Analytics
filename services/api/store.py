"""Persistence layer.

Two backends behind one interface:
  * InMemoryStore   — dependency-free, used by unit tests and as a dev fallback.
  * PostgresStore   — psycopg2 against the DDL in ddl.sql (NOT run in the
                      authoring env; no server/driver here — see BLOCKERS.md B-3).

The API talks only to the Store interface, so the ingest/ack logic is fully
tested against InMemoryStore without a database.
"""

from typing import Dict, List, Optional


class Store:
    def save_event(self, evt: dict) -> None: raise NotImplementedError
    def save_zone_state(self, state: dict) -> None: raise NotImplementedError
    def save_alert(self, alert: dict) -> str: raise NotImplementedError
    def list_alerts(self, unacked_only: bool = False) -> List[dict]: raise NotImplementedError
    def acknowledge_alert(self, alert_id: str, who: str, ts: float) -> bool: raise NotImplementedError
    def latest_zone_states(self) -> List[dict]: raise NotImplementedError
    def zone_state_range(self, zone_id: str, t0: float, t1: float) -> List[dict]: raise NotImplementedError


class InMemoryStore(Store):
    def __init__(self):
        self._events: List[dict] = []
        self._zone_ts: List[dict] = []
        self._alerts: Dict[str, dict] = {}
        self._alert_seq = 0

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
        self._alerts[alert_id] = rec
        return alert_id

    def list_alerts(self, unacked_only: bool = False) -> List[dict]:
        vals = list(self._alerts.values())
        if unacked_only:
            vals = [a for a in vals if a.get("acknowledged_by") is None]
        return sorted(vals, key=lambda a: a.get("ts", 0), reverse=True)

    def acknowledge_alert(self, alert_id: str, who: str, ts: float) -> bool:
        a = self._alerts.get(alert_id)
        if a is None or a.get("acknowledged_by") is not None:
            return False
        a["acknowledged_by"] = who
        a["acknowledged_at"] = ts
        return True

    def latest_zone_states(self) -> List[dict]:
        latest: Dict[str, dict] = {}
        for s in self._zone_ts:
            zid = s["zone_id"]
            if zid not in latest or s["ts"] >= latest[zid]["ts"]:
                latest[zid] = s
        return list(latest.values())

    def zone_state_range(self, zone_id: str, t0: float, t1: float) -> List[dict]:
        return [s for s in self._zone_ts
                if s["zone_id"] == zone_id and t0 <= s["ts"] <= t1]

    def event_count(self) -> int:
        return len(self._events)


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
