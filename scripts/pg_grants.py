#!/usr/bin/env python3
"""Give a read-only role the views, the safe columns of them, and nothing else.

    .venv/bin/python scripts/pg_grants.py --role finblade_readonly
    .venv/bin/python scripts/pg_grants.py --role finblade_readonly --dry-run
    .venv/bin/python scripts/pg_grants.py --role bot --dsn "$DATABASE_URL"

RUN IT AFTER scripts/pg_apply.py, EVERY TIME. Applying the views does DROP VIEW
followed by CREATE VIEW, and a dropped view takes its grants and its comments
with it. So the sequence is always:

    .venv/bin/python scripts/pg_apply.py   "$DATABASE_URL"
    .venv/bin/python scripts/pg_grants.py  --role finblade_readonly

Get that order wrong and the role silently loses access — the chatbot starts
returning permission errors and the cause is three commands upstream.

WHY COLUMN-LEVEL GRANTS. A text-to-SQL bot wrote COUNT(DISTINCT person_ref)
against this schema and got a plausible wrong number: person_ref is a hash of
the tracker id, so it counts track fragments, not people. Telling a model not to
do that is advice, and the prompt will eventually be edited by someone who was
not there for the explanation. Not granting the column makes the same query
fail with "permission denied", which is loud, and which the bot can react to.

The allowlist lives in services/api/analytics_views.py next to the views, so a
new column has to be considered rather than inherited.

WHY NO BASE TABLES, EVER. cameras.source holds RTSP URLs with embedded
passwords. A Postgres view runs with its OWNER's privileges, so a role granted
SELECT on the views needs no access at all to the tables underneath — and must
not have any. This script revokes table access before granting, then verifies.

It does NOT create the role and does not set passwords. Both belong to whoever
owns the credentials, not to a script in a repository.
"""

import argparse
import os
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO)

from services.api.analytics_views import (SAFE_COLUMNS,           # noqa: E402
                                          comment_sql, view_names)


def statements(role: str, schema: str = "public", database: str = None):
    """Every statement this will run, in order. Pure — builds no connection."""
    out = []
    if database:
        out.append(f'GRANT CONNECT ON DATABASE "{database}" TO "{role}"')
    out.append(f'GRANT USAGE ON SCHEMA {schema} TO "{role}"')

    # Clean slate first. A previous run, or a hand-typed GRANT, may have left
    # table-level access behind — and "GRANT SELECT on the six views" does not
    # remove a seventh grant somebody added last month.
    out.append(f"REVOKE ALL ON ALL TABLES IN SCHEMA {schema} FROM \"{role}\"")

    for view in view_names():
        cols = SAFE_COLUMNS.get(view)
        if not cols:
            # A view with no allowlist is not granted at all. Failing closed is
            # the only safe default: a new view is invisible until somebody
            # decides which of its columns may be read.
            continue
        collist = ", ".join(cols)
        out.append(f'GRANT SELECT ({collist}) ON {view} TO "{role}"')
    return out


def verify(conn, role: str, schema: str = "public"):
    """What the role can actually reach. Returns (views_ok, problems)."""
    problems = []

    granted = {}
    for v, c in conn.execute(
            "SELECT table_name, column_name FROM information_schema.column_privileges "
            "WHERE grantee = %s AND privilege_type = 'SELECT' AND table_schema = %s",
            (role, schema)).fetchall():
        granted.setdefault(v, set()).add(c)

    # Every base table must be unreachable, whatever else is true.
    base = {r[0] for r in conn.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = %s AND table_type = 'BASE TABLE'",
        (schema,)).fetchall()}
    for t in sorted(base & set(granted)):
        problems.append(f"base table {t} is readable by {role} "
                        f"({len(granted[t])} column(s)) — must be none")

    for view, cols in SAFE_COLUMNS.items():
        got = granted.get(view, set())
        missing = set(cols) - got
        extra = got - set(cols)
        if missing:
            problems.append(f"{view}: not granted {', '.join(sorted(missing))}")
        if extra:
            problems.append(f"{view}: granted more than the allowlist — "
                            f"{', '.join(sorted(extra))}")
    return granted, problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--role", required=True)
    ap.add_argument("--dsn", default=os.environ.get("DATABASE_URL"))
    ap.add_argument("--schema", default="public")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the statements and change nothing")
    ap.add_argument("--skip-comments", action="store_true")
    args = ap.parse_args()

    if not args.dsn:
        print("no --dsn and no DATABASE_URL")
        return 2

    import psycopg

    sql = statements(args.role, args.schema)
    comments = [] if args.skip_comments else comment_sql()

    if args.dry_run:
        print(f"-- would run against {args.dsn.split('@')[-1]}")
        for s in sql + comments:
            print(s + ";")
        print(f"-- {len(sql)} grant statement(s), {len(comments)} comment(s)")
        return 0

    with psycopg.connect(args.dsn, autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s",
                              (args.role,)).fetchone()
        if not exists:
            print(f"role {args.role!r} does not exist. Create it first — this "
                  f"script does not, because that means choosing a password:")
            print(f"  CREATE ROLE \"{args.role}\" LOGIN PASSWORD '...';")
            return 2

        for s in sql:
            conn.execute(s)
        print(f"applied {len(sql)} grant statement(s) for {args.role}")

        for s in comments:
            conn.execute(s)
        if comments:
            print(f"applied {len(comments)} comment(s)")

        granted, problems = verify(conn, args.role, args.schema)

    print()
    for view in sorted(SAFE_COLUMNS):
        n = len(granted.get(view, ()))
        print(f"  {view:<20} {n:>2} column(s)")

    if problems:
        print()
        print("PROBLEMS")
        for p in problems:
            print(f"  {p}")
        return 1

    print()
    print(f"{args.role} can read {len(SAFE_COLUMNS)} view(s) and no base table.")
    print("person_ref is not granted anywhere: COUNT(DISTINCT person_ref) now "
          "fails loudly instead of returning a plausible wrong number.")
    print()
    print("Re-run this after every scripts/pg_apply.py — DROP VIEW discards "
          "grants and comments.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
