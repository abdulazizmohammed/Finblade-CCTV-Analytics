"""A real uvicorn, backed by Postgres, answering real requests.

The suite exercises the routes through TestClient in-process. This starts the
actual server with DATABASE_URL set and drives it over HTTP — the deployment
shape, including the connection pool under concurrent requests, which an
in-process client never exercises.

Holds the cluster for the whole run in this process, because pgserver stops it
when its starter exits.
"""
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse as up
import urllib.request

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
os.chdir(REPO)
sys.path.insert(0, os.path.join(REPO, "scripts"))
import pg_conn                                                     # noqa: E402

import psycopg                                                     # noqa: E402

PORT = 8933
KEY = "pg-live-check"
BASE = f"http://127.0.0.1:{PORT}"

ok = True


def check(label, condition, detail=""):
    global ok
    ok = ok and bool(condition)
    print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))


def call(path, payload=None, params=None):
    if params:
        path += "?" + up.urlencode(params)
    req = urllib.request.Request(
        BASE + path,
        data=None if payload is None else json.dumps(payload).encode(),
        headers={"X-API-Key": KEY, "Content-Type": "application/json"},
        method="GET" if payload is None else "POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw or b"{}")
        except ValueError:
            return exc.code, {"_raw": raw[:200].decode("utf-8", "replace")}


dsn = pg_conn.dsn()
name = f"finblade_live_{os.getpid()}"
with psycopg.connect(dsn, autocommit=True) as admin:
    admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    admin.execute(f'CREATE DATABASE "{name}"')
app_dsn = up.urlunsplit(up.urlsplit(dsn)._replace(path="/" + name))
print(f"scratch database: {name}")

env = dict(os.environ, DATABASE_URL=app_dsn, FINBLADE_API_KEY=KEY,
           FINBLADE_AUTOSTART_CAMERAS="0")
env.pop("PYTHONPATH", None)
proc = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "services.api.app:app",
     "--host", "127.0.0.1", "--port", str(PORT), "--log-level", "warning"],
    env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

try:
    for _ in range(80):
        try:
            urllib.request.urlopen(f"{BASE}/healthz", timeout=1)
            break
        except Exception:                           # noqa: BLE001
            time.sleep(0.25)
    code, _ = call("/healthz")
    check("uvicorn came up on Postgres", code == 200)
    if code != 200:
        raise SystemExit(1)

    now = time.time()
    print("\n== ingest")
    codes = set()
    for i in range(20):
        c, _ = call("/api/v1/zones/state", {
            "zone_id": "ZONE-01", "camera_id": "CAM-01", "ts": now - 100 + i * 5,
            "occupancy": 3 if 5 <= i < 12 else 0, "density": 0.25,
            "capacity_pct": 7.5, "inflow_per_min": 0.0, "outflow_per_min": 0.0,
            "status": "NORMAL"})
        codes.add(c)
    check("zone-state posts accepted", codes == {202}, str(sorted(codes)))

    c, _ = call("/api/v1/events/ingest", {
        "event_id": "pg-e1", "event_type": "ZONE_ENTRY", "camera_id": "CAM-01",
        "site_id": "SITE-01", "zone_id": "ZONE-01", "zone_to": "ZONE-01",
        "person_ref": "pr_" + "a" * 16, "confidence": 0.9,
        "timestamp": now - 60, "occupancy": 3})
    check("event ingested", c == 202)

    c, alert = call("/api/v1/alerts", {
        "rule_id": "R-01", "severity": "WARNING", "message": "busy",
        "camera_id": "CAM-01", "zone_id": "ZONE-01", "ts": now - 50,
        "kind": "FIRE"})
    check("alert raised", c in (200, 201, 202), str(alert)[:120])
    alert_id = str(alert.get("alert_id") or "")

    print("\n== read paths")
    c, body = call("/api/v1/zones/state")
    check("live zone state", c == 200 and len(body.get("zones", [])) == 1,
          f"{c} {len(body.get('zones', []))}")

    c, body = call("/api/v1/summary")
    check("summary", c == 200 and "summary" in body, str(c))

    c, body = call("/api/v1/history/events", params={"from": 0, "to": now + 10})
    check("event history", c == 200 and len(body.get("events", [])) >= 1)

    c, body = call("/api/v1/zones/ZONE-01/series",
                   params={"from": now - 200, "to": now, "bucket": 30,
                           "camera_id": "CAM-01"})
    check("zone series on Postgres", c == 200 and body.get("points"),
          f"{c} {len(body.get('points', []))} buckets")

    c, body = call("/api/v1/zones/ZONE-01/duration",
                   params={"from": now - 200, "to": now, "camera_id": "CAM-01",
                           "field": "occupancy", "op": "gt", "value": 0})
    check("zone duration on Postgres", c == 200 and body.get("total_seconds", 0) > 0,
          f"{c} {body.get('total_seconds')}s occupied")

    c, body = call("/api/v1/reports/occupancy.json",
                   params={"from": now - 200, "to": now})
    zone = (body.get("zones") or [{}])[0]
    check("occupancy report", c == 200 and zone.get("avg_occupancy") is not None,
          f"avg={zone.get('avg_occupancy')} coverage={zone.get('coverage')}")

    if alert_id:
        c, _ = call(f"/api/v1/alerts/{alert_id}/ack", {"acknowledged_by": "ana"})
        check("alert acknowledge writes through", c == 200, str(c))

    c, body = call("/api/v1/health")
    checks = body.get("checks", {})
    check("health reports the store as ok",
          checks.get("store", {}).get("ok") is True, str(checks.get("store")))

    print("\n== the connection pool under concurrent load")
    # An in-process TestClient never exercises this: a single connection behind
    # a lock would serialise here and a pool that leaks would exhaust.
    results = []

    def hammer():
        for _ in range(15):
            code, _b = call("/api/v1/summary")
            results.append(code)

    threads = [threading.Thread(target=hammer) for _ in range(8)]
    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    secs = time.time() - t0
    check("120 concurrent requests all succeeded",
          len(results) == 120 and set(results) == {200},
          f"{len(results)} in {secs:.1f}s, codes={sorted(set(results))}")

finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    print(f"dropped {name}")

print("\n" + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
sys.exit(0 if ok else 1)
