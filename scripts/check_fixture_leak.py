"""Do the credential fixtures in the repo match a REAL camera?

redact.py and test_credential_redaction.py carry an example URL that came from
live data — it is why the "@ in the password" bug was found. The question is
how much of the real thing came with it.

Prints only match / no-match and lengths. Never prints a password, and never
writes anything.
"""
import os
import re
import sqlite3
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
os.chdir(REPO)

FIXTURE = "rtsp://admin:Secret@2030@192.168.200.2:554/Streaming/Channels/102"
m = re.match(r"rtsp://([^:]+):(.*)@([\d.]+):", FIXTURE)
f_user, f_pass, f_host = m.group(1), m.group(2), m.group(3)
print(f"fixture in the repo: user={f_user!r} host={f_host} "
      f"password={len(f_pass)} chars, ends {f_pass[-5:]!r}")

db = "data/finblade.db"
if not os.path.exists(db):
    print("\nno local database to compare against")
    raise SystemExit(0)

conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
rows = conn.execute(
    "SELECT camera_id, source FROM cameras WHERE source LIKE 'rtsp://%'").fetchall()

print(f"\ncomparing against {len(rows)} registered camera(s):")
hit = False
for camera_id, source in rows:
    mm = re.match(r"rtsp://([^:]+):(.*)@([\d.]+):", source or "")
    if not mm:
        continue
    user, pw, host = mm.group(1), mm.group(2), mm.group(3)
    same_host = host == f_host
    same_user = user == f_user
    same_pw = pw == f_pass
    tail = pw[-5:] == f_pass[-5:]
    flags = []
    if same_host:
        flags.append("SAME HOST")
    if same_user:
        flags.append("same user")
    if same_pw:
        flags.append("SAME PASSWORD")
    elif tail:
        flags.append("SAME PASSWORD TAIL")
    if flags:
        hit = True
    print(f"  {camera_id}: {', '.join(flags) if flags else 'no overlap'}")

print()
if hit:
    print("VERDICT: the committed fixture overlaps a real camera.")
    print("  A reachable internal IP and part of a working credential are in")
    print("  git history, which a private repo limits but does not undo.")
else:
    print("VERDICT: no overlap with any registered camera.")
