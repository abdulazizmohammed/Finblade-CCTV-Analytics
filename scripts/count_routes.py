"""How many routes does the service actually expose?

The client guide states a number. Stating it wrong is the kind of small
inaccuracy that makes a reader distrust the rest of the document.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
os.environ.setdefault("FINBLADE_AUTOSTART_CAMERAS", "0")

from services.api.app import app                              # noqa: E402

api, other = [], []
for route in app.routes:
    path = getattr(route, "path", None)
    if not path:
        continue
    methods = sorted(getattr(route, "methods", []) or ["WS"])
    (api if path.startswith("/api/v1") else other).append(
        f"{','.join(m for m in methods if m not in ('HEAD', 'OPTIONS')):<12} {path}")

for line in sorted(api):
    print(line)
print()
for line in sorted(other):
    print(line)
print(f"\n/api/v1 routes: {len(api)}")
print(f"other routes  : {len(other)}")
print(f"total         : {len(api) + len(other)}")
