"""Which tracked scripts does an EC2 deployment actually need?

`repo_audit.py` found 102 of 120 tracked scripts unreferenced by the runtime.
Unreferenced is not the same as unneeded — a deploy script is run by a human,
not imported — so this sorts them by what they are FOR, using the deployment
docs as the authority on which ones a human is told to run.

Read-only.
"""
import os
import re
import subprocess
import sys
from collections import defaultdict

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
os.chdir(REPO)

tracked = subprocess.run(["git", "ls-files", "scripts/"],
                         capture_output=True, text=True).stdout.split()

# What the docs and the README tell a human to run. These are needed on the
# server whether or not any code imports them.
doc_text = ""
for path in ("README.md", "docs/DEPLOY.md", "docs/FINBLADE_CLIENT_GUIDE.md",
             "docs/FINBLADE_API_REQUIREMENTS.md", "integrations/finblade_ai/README.md",
             "MORNING.md", "BLOCKERS.md", "DECISIONS.md", "CLAUDE.md"):
    if os.path.exists(path):
        with open(path, errors="ignore") as fh:
            doc_text += fh.read()

# Scripts referenced by other scripts still matter — a runner needs its helper.
script_text = ""
for s in tracked:
    if os.path.exists(s):
        with open(s, errors="ignore") as fh:
            script_text += fh.read()

RUNTIME = ("deploy", "install", "start", "stop", "status", "verify", "restart",
           "backup", "service", "preflight", "get_weights", "bootstrap",
           "rtsp", "pg_", "smoke", "upgrade", "push", "health")

buckets = defaultdict(list)
for s in sorted(tracked):
    base = os.path.basename(s)
    stem = os.path.splitext(base)[0]
    in_docs = base in doc_text
    # Referenced by another script (not counting itself).
    others = script_text.replace(
        open(s, errors="ignore").read() if os.path.exists(s) else "", "")
    in_scripts = base in others

    if in_docs:
        buckets["documented — a human is told to run it"].append(s)
    elif in_scripts:
        buckets["called by another script"].append(s)
    elif any(k in stem for k in RUNTIME):
        buckets["looks operational, but nothing references it"].append(s)
    else:
        buckets["one-off: checks, probes, benchmarks, experiments"].append(s)

for title in ("documented — a human is told to run it",
              "called by another script",
              "looks operational, but nothing references it",
              "one-off: checks, probes, benchmarks, experiments"):
    items = buckets.get(title, [])
    print(f"\n== {title}  ({len(items)})")
    for s in items:
        size = os.path.getsize(s) if os.path.exists(s) else 0
        print(f"   {size / 1024:>6.1f} KB  {s}")

total = sum(os.path.getsize(s) for s in tracked if os.path.exists(s))
print(f"\n{len(tracked)} scripts, {total / 1024:.0f} KB total")
