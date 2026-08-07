"""Read-only: what cameras are registered, and what source each one uses.

Sources are masked through services/api/redact.py — the same path the API uses
— because a camera row holds RTSP URLs with the password in them.
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from services.api.redact import mask_credentials                   # noqa: E402

db = sys.argv[1] if len(sys.argv) > 1 else "data/finblade.db"
if not os.path.exists(db):
    print(f"no database at {db}")
    raise SystemExit(0)

conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row
rows = conn.execute(
    "SELECT camera_id, name, site_id, state, enabled, source, stream_url, "
    "last_seen FROM cameras ORDER BY camera_id").fetchall()

if not rows:
    print("no cameras registered")
    raise SystemExit(0)

print(f"{'camera_id':<12} {'name':<18} {'state':<10} {'source'}")
print("-" * 92)
for r in rows:
    src = mask_credentials(r["source"] or "") or "(none)"
    print(f"{r['camera_id']:<12} {str(r['name'] or ''):<18} "
          f"{str(r['state'] or '?'):<10} {src}")

print(f"\n{len(rows)} camera(s)")
