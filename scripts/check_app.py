import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import services.api.app as a
print("app import OK:", type(a.app).__name__)
routes = [getattr(r, "path", "") for r in a.app.routes]
for p in ["/api/v1/events/ingest", "/api/v1/zones/state", "/api/v1/alerts",
          "/api/v1/reports/occupancy", "/ws"]:
    print(("  OK " if p in routes else "  MISSING "), p)
print("web mounted:", any(getattr(r, "path", "") == "/web" for r in a.app.routes))
