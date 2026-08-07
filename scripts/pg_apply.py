"""Apply the generated schema and the analytics views to a real Postgres.

Proves three things that only a live server can:
  * the generated DDL parses and creates every table SQLite has;
  * the view SQL is genuinely portable — same definitions, Postgres dialect;
  * the chatbot's queries run there.

Usage:
  PYTHONPATH=.pgtest .venv/bin/python scripts/pg_apply.py [dsn]
DSN defaults to the cluster scripts/pg_local_install.sh started.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pg_conn                                                     # noqa: E402

from services.api.analytics_views import (POSTGRES, drop_sql,      # noqa: E402
                                          view_definitions)

dsn = pg_conn.dsn(sys.argv[1] if len(sys.argv) > 1 else None)

ddl_path = "services/api/ddl_pg.sql"
if not os.path.exists(ddl_path):
    print(f"{ddl_path} missing — run scripts/gen_pg_ddl.py first")
    raise SystemExit(1)

ok = True


def check(label, condition, detail=""):
    global ok
    ok = ok and bool(condition)
    print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))


with pg_conn.connect(dsn) as conn:
    print("connected:", conn.execute("SELECT version()").fetchone()[0][:60])

    print("\n== applying generated DDL")
    try:
        conn.execute(open(ddl_path).read())
        check("ddl_pg.sql parses and applies", True)
    except Exception as exc:                        # noqa: BLE001
        check("ddl_pg.sql parses and applies", False, str(exc)[:200])
        raise SystemExit(1)

    tables = [r[0] for r in conn.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='public' AND table_type='BASE TABLE' ORDER BY 1")]
    print("  tables:", ", ".join(tables))
    expected = {"alerts", "cameras", "events", "forwarder_cursors", "reports",
                "zone_live", "zone_state_ts", "zones"}
    check("every SQLite table exists in Postgres", expected <= set(tables),
          f"missing {sorted(expected - set(tables))}")

    print("\n== creating the analytics views (postgres dialect)")
    for stmt in drop_sql(POSTGRES):
        conn.execute(stmt)
    created = []
    for name, sql in view_definitions(POSTGRES):
        try:
            conn.execute(sql)
            created.append(name)
        except Exception as exc:                    # noqa: BLE001
            check(f"view {name}", False, str(exc)[:250])
    check("all five views created on Postgres", len(created) == 5,
          f"{created}")

    print("\n== the views are queryable (empty tables, but the SQL must run)")
    for name in created:
        try:
            n = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            print(f"  {name:<20} {n:>10,} rows")
        except Exception as exc:                    # noqa: BLE001
            check(f"select from {name}", False, str(exc)[:200])

    print("\n== a chatbot query shape runs on Postgres")
    try:
        conn.execute("""
            SELECT zone_name, camera_id,
                   SUM(CASE WHEN occupancy > 0 AND NOT is_stale
                            THEN duration_seconds ELSE 0 END) / 60.0 AS busy_minutes
            FROM v_zone_intervals
            WHERE valid_from_utc >= now() - interval '24 hours'
            GROUP BY camera_id, zone_id, zone_name
            ORDER BY busy_minutes DESC NULLS LAST""").fetchall()
        check("interval + timestamptz query runs", True)
    except Exception as exc:                        # noqa: BLE001
        check("interval + timestamptz query runs", False, str(exc)[:250])

print("\n" + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
sys.exit(0 if ok else 1)
