"""Run the ENTIRE test suite with the application backed by Postgres.

The conformance suite proves the three stores agree on their own behaviour.
This proves the app works on top of the Postgres one — routes, rule engine,
forwarder, report scheduler — against a real server.

ONE process holds the server for the whole run. pgserver shuts its cluster down
when the process that started it exits, so a shell script that started it in
one python and connected from the next found the socket already gone.

A scratch database per run, dropped afterwards, so this never touches the
migrated data in the default database.
"""
import os
import subprocess
import sys
import urllib.parse as up

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
os.chdir(REPO)
sys.path.insert(0, os.path.join(REPO, "scripts"))
import pg_conn                                                     # noqa: E402

import psycopg                                                     # noqa: E402

dsn = pg_conn.dsn()
name = f"finblade_suite_{os.getpid()}"

with psycopg.connect(dsn, autocommit=True) as admin:
    admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    admin.execute(f'CREATE DATABASE "{name}"')
print(f"created scratch database {name}")

parts = up.urlsplit(dsn)
suite_dsn = up.urlunsplit(parts._replace(path="/" + name))
print(f"suite DSN: {suite_dsn}\n")

env = dict(os.environ, DATABASE_URL=suite_dsn)
# The app venv has psycopg; .pgtest is only needed for pgserver, which the
# suite does not use.
env.pop("PYTHONPATH", None)

print("=== full suite, application backed by Postgres")
proc = subprocess.run(
    [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"],
    env=env, capture_output=True, text=True, timeout=1800)

log = os.path.join(REPO, "scripts", "logs", "suite_pg.log")
os.makedirs(os.path.dirname(log), exist_ok=True)
with open(log, "w") as fh:
    fh.write(proc.stdout + "\n" + proc.stderr)

tail = (proc.stdout + proc.stderr).strip().splitlines()
print("\n".join(tail[-6:]))
print(f"\n(full log: {log})")

with psycopg.connect(dsn, autocommit=True) as admin:
    admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
print(f"dropped {name}")

sys.exit(proc.returncode)
