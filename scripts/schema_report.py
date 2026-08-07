"""Read-only: what the live SQLite database actually contains.

Written to answer "should we move to Postgres" with the current shape rather
than a remembered one. Changes nothing.
"""
import os
import sqlite3
import sys

db = sys.argv[1] if len(sys.argv) > 1 else "data/finblade.db"
if not os.path.exists(db):
    print(f"no database at {db}")
    raise SystemExit(0)

print(f"{db}: {os.path.getsize(db) / 1e9:.2f} GB")
conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
tables = [r[0] for r in conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
print(f"tables: {len(tables)}\n")
for t in tables:
    n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    cols = len(list(conn.execute(f"PRAGMA table_info({t})")))
    print(f"  {t:<22} {n:>12,} rows  {cols:>3} cols")

print()
for pragma in ("journal_mode", "page_size", "page_count"):
    print(f"  {pragma:<14} {conn.execute('PRAGMA ' + pragma).fetchone()[0]}")

cams = conn.execute("SELECT COUNT(DISTINCT camera_id) FROM zone_state_ts").fetchone()[0]
zones = conn.execute(
    "SELECT COUNT(*) FROM (SELECT DISTINCT camera_id, zone_id FROM zone_state_ts)"
).fetchone()[0]
print(f"\n  cameras that have reported : {cams}")
print(f"  distinct camera+zone pairs : {zones}")
