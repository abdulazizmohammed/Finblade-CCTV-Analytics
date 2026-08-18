"""Is this Postgres schema current for the code checked out right now?

    .venv/bin/python scripts/db_schema_check.py --dsn "$DATABASE_URL"

WHY IT DOES NOT HOLD A LIST OF EXPECTED TABLES. Any hardcoded expectation drifts
the first time somebody adds a column and forgets this file, and a check that has
quietly stopped checking is worse than none. It applies services/api/ddl_pg.sql
into a throwaway schema, diffs the live database against that, and drops the
scratch. Applying the real DDL rather than parsing it matters: a parser is a
second implementation of the schema and drifts from the first.

Read-only on the database you point it at, and it repairs nothing. Migration
belongs to the API at startup, which is the one place that should touch a schema.

Exit codes: 0 current, 1 behind, 2 could not check.
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def check_postgres(dsn) -> int:
    """Compare a live Postgres against ddl_pg.sql applied to a scratch schema.

    Applying the DDL into a throwaway schema and diffing beats parsing it: a
    parser is a second implementation of the schema and drifts from the first.
    """
    import psycopg
    ddl_path = os.path.join(os.path.dirname(__file__), "..",
                            "services", "api", "ddl_pg.sql")
    if not os.path.exists(ddl_path):
        print("services/api/ddl_pg.sql missing - it is the schema authority "
              "and must be present")
        return 2

    scratch = "fb_schema_ref"
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(f"DROP SCHEMA IF EXISTS {scratch} CASCADE")
            conn.execute(f"CREATE SCHEMA {scratch}")
            conn.execute(f"SET search_path TO {scratch}")
            with open(ddl_path) as fh:
                conn.execute(fh.read())
            want, want_ix = {}, set()
            for t, c in conn.execute(
                    "SELECT table_name, column_name FROM information_schema.columns "
                    "WHERE table_schema = %s", (scratch,)).fetchall():
                want.setdefault(t, set()).add(c)
            conn.execute(f"DROP SCHEMA {scratch} CASCADE")
    except Exception as exc:                       # noqa: BLE001
        print(f"could not build the reference schema: {exc.__class__.__name__}: {exc}")
        return 2

    have, _have_ix = pg_schema(dsn)

    missing_tables = sorted(set(want) - set(have))
    missing_cols = {t: sorted(want[t] - have[t])
                    for t in sorted(set(want) & set(have)) if want[t] - have[t]}
    extra = sorted(set(have) - set(want))

    print(f"database : postgres, {dsn.split('@')[-1]}")
    print(f"tables   : {len(have)} present, {len(want)} expected")

    if not (missing_tables or missing_cols):
        print()
        print("UP TO DATE — every table and column the current code expects "
              "is present.")
        if extra:
            print(f"  (also holds {len(extra)} table(s) the code no longer uses: "
                  f"{', '.join(extra)} — harmless)")
        return 0

    print()
    print("BEHIND — this database is missing:")
    for t in missing_tables:
        print(f"  table   {t}")
    for t, cols in missing_cols.items():
        for c in cols:
            print(f"  column  {t}.{c}")
    print()
    print("Fix: apply the DDL, which is idempotent and includes ADD COLUMN IF "
          "NOT EXISTS for every column:")
    print("  psql \"$DATABASE_URL\" -f services/api/ddl_pg.sql")
    print("The API also applies it at startup, so a restart is equivalent.")
    return 1


def pg_schema(dsn):
    """{table: {column, ...}} and index names, from a live Postgres.

    BASE TABLE only. information_schema.columns covers views too, and a
    deployment that has had scripts/pg_apply.py run against it holds five —
    v_zone_current and friends. Counting those as tables made the summary line
    read "13 present, 13 expected" on a database that was missing five tables
    and holding five views, which is exactly the arithmetic that hides a
    problem instead of showing it.
    """
    import psycopg
    tables, indexes = {}, set()
    with psycopg.connect(dsn) as conn:
        base = {r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = current_schema() "
            "AND table_type = 'BASE TABLE'").fetchall()}
        for t, c in conn.execute(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema()").fetchall():
            if t in base:
                tables.setdefault(t, set()).add(c)
        for (n,) in conn.execute(
                "SELECT indexname FROM pg_indexes "
                "WHERE schemaname = current_schema()").fetchall():
            indexes.add(n)
    return tables, indexes


def main() -> int:
    dsn = None
    if "--dsn" in sys.argv:
        dsn = sys.argv[sys.argv.index("--dsn") + 1]
    else:
        dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("no --dsn and no DATABASE_URL.")
        print("Postgres is the only backend; SQLite has been removed.")
        print("  bash scripts/pg_dev.sh start")
        return 2
    return check_postgres(dsn)


if __name__ == "__main__":
    raise SystemExit(main())
