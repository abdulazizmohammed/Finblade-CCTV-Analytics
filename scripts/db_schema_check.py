#!/usr/bin/env python3
"""Is this database's schema current for the code checked out right now?

    .venv/bin/python scripts/db_schema_check.py                # data/finblade.db
    .venv/bin/python scripts/db_schema_check.py path/to.db

WHY THIS IS NOT A LIST OF EXPECTED TABLES. Any hardcoded expectation drifts the
first time someone adds a column and forgets to update it here, and a schema
check that quietly stops checking is worse than none. So the target is built by
creating a throwaway database with the SQLiteStore in this working tree —
whatever `_SCHEMA` plus `_migrate()` produces IS by definition current — and the
live file is compared against it. The check cannot fall behind the code.

WHAT IT DOES NOT DO. It never writes to the database you point it at; it opens
it read-only. It reports, it does not repair. Migration happens on its own when
the API starts, which is the only place that should be touching the schema.

Exit codes: 0 current, 1 behind, 2 could not check.
"""

import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def schema_of(conn):
    """{table: {column, ...}} for every real table."""
    out = {}
    for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"):
        out[name] = {r[1] for r in conn.execute(f"PRAGMA table_info({name})")}
    return out


def indexes_of(conn):
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND name NOT LIKE 'sqlite_%'")}


def main() -> int:
    live_path = sys.argv[1] if len(sys.argv) > 1 else "data/finblade.db"
    if not os.path.exists(live_path):
        print(f"no database at {live_path} — it is created on first API start")
        return 2

    # The target: what the code in this working tree produces from nothing.
    tmpdir = tempfile.mkdtemp()
    try:
        from services.api.sqlite_store import SQLiteStore
        ref_path = os.path.join(tmpdir, "reference.db")
        SQLiteStore(ref_path)                      # runs _SCHEMA and _migrate()
        ref = sqlite3.connect(ref_path)
        want, want_ix = schema_of(ref), indexes_of(ref)
        ref.close()
    except Exception as exc:                       # noqa: BLE001
        print(f"could not build the reference schema: {exc.__class__.__name__}: {exc}")
        return 2

    live = sqlite3.connect(f"file:{live_path}?mode=ro", uri=True)
    have, have_ix = schema_of(live), indexes_of(live)
    live.close()

    missing_tables = sorted(set(want) - set(have))
    missing_cols = {t: sorted(want[t] - have[t])
                    for t in sorted(set(want) & set(have))
                    if want[t] - have[t]}
    missing_ix = sorted(want_ix - have_ix)
    # Extra tables are not a fault. A database that has run older code keeps
    # things since removed, and nothing reads them — worth showing, never a
    # failure.
    extra_tables = sorted(set(have) - set(want))

    print(f"database : {live_path}")
    print(f"tables   : {len(have)} present, {len(want)} expected")

    if not (missing_tables or missing_cols or missing_ix):
        print()
        print("UP TO DATE — every table, column and index the current code "
              "expects is present.")
        if extra_tables:
            print(f"  (also holds {len(extra_tables)} table(s) the code no longer "
                  f"uses: {', '.join(extra_tables)} — harmless)")
        return 0

    print()
    print("BEHIND — this database is missing:")
    for t in missing_tables:
        print(f"  table   {t}")
    for t, cols in missing_cols.items():
        for c in cols:
            print(f"  column  {t}.{c}")
    for i in missing_ix:
        print(f"  index   {i}")
    print()
    print("Fix: restart the API. SQLiteStore runs _migrate() at construction, "
          "which adds these in place without touching existing rows. Then run "
          "this again.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
