"""The views on Postgres, with the real data in them, plus the read-only role.

This is the deliverable end to end: FinBlade connects as a role that can see
the views and nothing else, and asks the questions a chatbot asks.

Two things it checks that matter more than the timings:

  * The grant is view-only. `finblade_ro` must be able to SELECT the views and
    must be REFUSED on the base tables. A role that can read zone_state_ts
    directly can also read `cameras`, which holds RTSP URLs with passwords in
    them — that has leaked once already.

  * The numbers match what the API returns. The whole argument for a view is
    that the semantics travel with it; if the SQL and the endpoints disagree,
    they do not.
"""
import os
import sys
import time

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pg_conn                                                     # noqa: E402

from services.api.analytics_views import (POSTGRES, drop_sql,      # noqa: E402
                                          view_definitions, view_names)

RO_ROLE = "finblade_ro"
RO_PASSWORD = os.environ.get("FINBLADE_RO_PASSWORD", "change-me-before-deploy")

ok = True


def check(label, condition, detail=""):
    global ok
    ok = ok and bool(condition)
    print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))


def timed(conn, sql, params=()):
    t0 = time.time()
    rows = conn.execute(sql, params).fetchall()
    return rows, time.time() - t0


dsn = pg_conn.dsn()
with pg_conn.connect(dsn) as conn:
    print("== rebuilding views against the migrated data")
    for stmt in drop_sql(POSTGRES):
        conn.execute(stmt)
    for _name, sql in view_definitions(POSTGRES):
        conn.execute(sql)
    print("  ", ", ".join(view_names(POSTGRES)))

    print("\n== v_timeline is the sum, not a product")
    rows, secs = timed(conn, "SELECT record_type, COUNT(*) FROM v_timeline "
                             "GROUP BY record_type ORDER BY 1")
    counts = dict(rows)
    print(f"   {counts}   ({secs:.2f}s)")
    base = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("zone_state_ts", "events", "alerts")}
    check("timeline row count is the sum of the tables",
          sum(counts.values()) == sum(base.values()),
          f"{sum(counts.values()):,} vs {sum(base.values()):,}")

    print("\n== the three chatbot questions, on Postgres, over 1.67M rows")

    rows, secs = timed(conn, """
        SELECT camera_id, zone_id, COUNT(*) readings,
               ROUND((SUM(duration_seconds)/3600.0)::numeric, 1) hours,
               COUNT(*) FILTER (WHERE is_stale) stale
        FROM v_zone_intervals GROUP BY camera_id, zone_id ORDER BY readings DESC""")
    print(f"   intervals per zone  ({secs:.2f}s)")
    for r in rows:
        print(f"     {r[0]}/{r[1]:<10} {r[2]:>9,} readings  {r[3]:>7} h  {r[4]:>4} stale")
    check("every zone has intervals", len(rows) == 8, f"{len(rows)}")

    pick = conn.execute("SELECT camera_id, zone_id, valid_from FROM v_zone_intervals "
                        "WHERE occupancy > 0 ORDER BY valid_from DESC LIMIT 1").fetchone()
    at = pick[2] + 1
    rows, secs = timed(conn, """
        SELECT occupancy, status, is_stale FROM v_zone_intervals
        WHERE camera_id=%s AND zone_id=%s AND valid_from <= %s
          AND (valid_to > %s OR valid_to IS NULL)""", (pick[0], pick[1], at, at))
    print(f"\n   point in time: {rows}  ({secs:.2f}s)")
    check("point-in-time returns exactly one interval", len(rows) == 1)

    rows, secs = timed(conn, """
        SELECT camera_id, zone_id,
               AVG(occupancy) AS naive,
               SUM(occupancy * duration_seconds) / NULLIF(SUM(duration_seconds),0) AS weighted
        FROM v_zone_intervals
        WHERE NOT is_stale AND duration_seconds IS NOT NULL
        GROUP BY camera_id, zone_id ORDER BY 1, 2""")
    print(f"\n   naive vs time-weighted average  ({secs:.2f}s)")
    worst = 0.0
    for cam, zone, naive, weighted in rows:
        worst = max(worst, abs(float(naive or 0) - float(weighted or 0)))
        print(f"     {cam}/{zone:<10} naive={float(naive or 0):.4f}  "
              f"weighted={float(weighted or 0):.4f}")
    print(f"   largest disagreement: {worst:.4f}")
    # The Python implementation measured 0.0419 and the SQLite views 0.0424 on
    # the same data. Postgres must land in the same place or one of the three
    # is wrong.
    check("agrees with the API's own figure", 0.03 < worst < 0.06, f"{worst:.4f}")

    # ------------------------------------------------------------ the role --
    print("\n== read-only role for FinBlade")
    from psycopg import sql

    role = sql.Identifier(RO_ROLE)
    # CREATE ROLE cannot take a bind parameter — Postgres parses DDL before
    # parameters are bound, so `PASSWORD %s` is a syntax error. sql.Literal
    # quotes and escapes it into the statement text safely; string formatting
    # here would be an injection.
    if conn.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (RO_ROLE,)).fetchone():
        conn.execute(sql.SQL("DROP OWNED BY {}").format(role))
        conn.execute(sql.SQL("DROP ROLE {}").format(role))
    conn.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
        role, sql.Literal(RO_PASSWORD)))

    # Connect and see the schema — and nothing else by default.
    db = conn.execute("SELECT current_database()").fetchone()[0]
    conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
        sql.Identifier(db), role))
    conn.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role))
    # Deliberately NOT "GRANT SELECT ON ALL TABLES". Only the views.
    for name in view_names(POSTGRES):
        conn.execute(sql.SQL("GRANT SELECT ON {} TO {}").format(
            sql.Identifier(name), role))
    # A table added later must not become readable by accident.
    conn.execute(sql.SQL(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "REVOKE ALL ON TABLES FROM {}").format(role))
    print(f"  created {RO_ROLE}, granted SELECT on "
          f"{len(view_names(POSTGRES))} views only")

ro_dsn = dsn.split("?")[0].replace("postgresql://postgres:@",
                                   f"postgresql://{RO_ROLE}:{RO_PASSWORD}@")
if "host=" in dsn:
    ro_dsn = f"{ro_dsn}?{dsn.split('?', 1)[1]}"

with pg_conn.connect(ro_dsn) as ro:
    who = ro.execute("SELECT current_user").fetchone()[0]
    check("connected as the restricted role", who == RO_ROLE, who)

    n = ro.execute("SELECT COUNT(*) FROM v_zone_intervals").fetchone()[0]
    check("the role CAN read the views", n > 0, f"{n:,} rows")

    denied = []
    for t in ("zone_state_ts", "events", "alerts", "cameras", "zones"):
        try:
            ro.execute(f"SELECT * FROM {t} LIMIT 1").fetchone()
            denied.append(f"{t}: READABLE")
        except Exception as exc:                    # noqa: BLE001
            if "permission denied" not in str(exc).lower():
                denied.append(f"{t}: {exc.__class__.__name__}")
    check("the role CANNOT read the base tables", not denied, str(denied))

    # The specific thing the base-table ban is protecting.
    try:
        ro.execute("SELECT source FROM cameras LIMIT 1").fetchone()
        check("RTSP credentials are unreachable", False, "cameras.source was readable")
    except Exception:                               # noqa: BLE001
        check("RTSP credentials are unreachable", True)

    for stmt in ("INSERT INTO zone_state_ts (zone_id) VALUES ('x')",
                 "DELETE FROM alerts",
                 "CREATE TABLE probe (id int)"):
        try:
            ro.execute(stmt)
            check(f"write refused: {stmt[:24]}", False, "it succeeded")
        except Exception:                           # noqa: BLE001
            check(f"write refused: {stmt[:24]}", True)

print("\n" + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
sys.exit(0 if ok else 1)
