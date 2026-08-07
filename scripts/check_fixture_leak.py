"""Does any credential-shaped fixture in the repo match a REAL camera?

Scans tracked files for rtsp URLs carrying userinfo and compares each against
the registered cameras. Reports overlap only — never prints a password.

WHY THIS TAKES NO HARDCODED URL. The first version of this script held the
offending fixture as a constant so it could compare against it, which put the
live camera's address, username and password tail back into the repo in the
very commit that removed it from two other files. prepush_check.sh caught it.
The value to compare against must come from the working tree at run time, not
from this file.

Read-only.
"""
import os
import re
import sqlite3
import subprocess
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
os.chdir(REPO)

# scheme://user:password@host — the shape that carries a secret. Matches the
# greedy form redact.py uses, so a password containing '@' is captured whole.
URL = re.compile(r"rtsp://(?P<user>[^:/\s]+):(?P<pw>[^\s]*)@(?P<host>[\w.\-]+)")

# Placeholders we deliberately document the masking with. Flagging these makes
# the check noise, and a noisy check gets ignored.
PLACEHOLDER = re.compile(r"\$\{|\[\^|\(\?|\*\*\*|user:pass|user:password|"
                         r"operator:p@ssw0rd|admin:\$")

tracked = subprocess.run(["git", "ls-files"], capture_output=True,
                         text=True).stdout.split()

found = []
for path in tracked:
    if not os.path.exists(path) or path == os.path.relpath(__file__, REPO):
        continue
    try:
        with open(path, errors="ignore") as fh:
            body = fh.read()
    except OSError:
        continue
    for line_no, line in enumerate(body.splitlines(), 1):
        if PLACEHOLDER.search(line):
            continue
        m = URL.search(line)
        if m:
            found.append((path, line_no, m.group("user"), m.group("pw"),
                          m.group("host")))

print(f"credential-shaped URLs in tracked files: {len(found)}")
for path, line_no, user, pw, host in found:
    print(f"  {path}:{line_no}  user={user!r} host={host} "
          f"password={len(pw)} chars")

db = "data/finblade.db"
if not os.path.exists(db):
    print("\nno local database — cannot say whether these are real")
    raise SystemExit(0)

conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
cameras = []
for camera_id, source in conn.execute(
        "SELECT camera_id, source FROM cameras WHERE source LIKE 'rtsp://%'"):
    m = URL.search(source or "")
    if m:
        cameras.append((camera_id, m.group("user"), m.group("pw"), m.group("host")))

print(f"\ncomparing against {len(cameras)} registered camera(s)")
overlap = False
for path, line_no, user, pw, host in found:
    for camera_id, c_user, c_pw, c_host in cameras:
        # A shared USERNAME is not a leak. "admin" is the default on every
        # camera ever made, and reporting it made this check print 17 lines of
        # noise around the two that mattered. Only an address or password
        # overlap says a real value escaped.
        flags = []
        if host == c_host:
            flags.append("SAME HOST")
        if pw == c_pw:
            flags.append("SAME PASSWORD")
        elif len(pw) >= 4 and pw[-4:] == c_pw[-4:]:
            flags.append("SAME PASSWORD TAIL")
        if flags:
            overlap = True
            if user == c_user:
                flags.append("(and same user)")
            print(f"  {path}:{line_no} vs {camera_id}: {', '.join(flags)}")

print()
if overlap:
    print("VERDICT: a tracked fixture overlaps a real camera. Rotate the")
    print("  credential; removing it from HEAD does not clear git history.")
    sys.exit(1)
print("VERDICT: no tracked fixture matches a registered camera.")
