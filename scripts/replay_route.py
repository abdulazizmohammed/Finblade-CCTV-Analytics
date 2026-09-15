#!/usr/bin/env python3
"""Drive a simulated vehicle between two branches, through the real ingest.

    .venv/bin/python scripts/replay_route.py --from RUH-01 --to KHJ-01
    .venv/bin/python scripts/replay_route.py --from RUH-01 --to KHJ-01 --speed 90 --interval 2 --time-scale 20
    .venv/bin/python scripts/replay_route.py --gpx media/route.gpx --id VEH-DEMO-1
    .venv/bin/python scripts/replay_route.py --from RUH-01 --to RUH-02 --loop

For the demo without a vehicle on the road. It posts positions to
POST /api/v1/trackers/ingest in the JSON dialect, exactly as a phone would, so
everything downstream — tracker_live, geofence arrival/departure events, the
map dot, R-12 — is the shipping path. Nothing is faked inside the server.

WHAT IT DRAWS. With --from/--to it looks up the two branches' coordinates
from the API and drives a gentle great-circle-ish line between them with a
slight curve so it does not look like a ruler, dwelling --dwell seconds at
each end so the geofence sees an arrival and a departure. With --gpx it
replays a recorded track (any <trkpt lat lon> file) point by point.

--time-scale N makes simulated time run N× faster than wall time, so an
80 km drive at 90 km/h takes about 2.5 min on stage instead of 53. Reported
timestamps are the simulated ones, spaced --interval seconds apart.
"""

import argparse
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO)

from finblade.gps import haversine_m                       # noqa: E402


def api(url, key, path, body=None):
    req = urllib.request.Request(url.rstrip("/") + path,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 method="POST" if body is not None else "GET",
                                 headers={"Content-Type": "application/json"})
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def branch_coords(url, key):
    idx = api(url, key, "/api/v1/org/index")
    return {b["branch_id"]: (b["lat"], b["lon"], b.get("name") or b["branch_id"])
            for b in idx["branches"] if b.get("lat") is not None}


def bearing(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def synth_route(a, b, speed_kmh, interval_s, dwell_s):
    """(lat, lon, speed_kmh) samples: dwell, drive with a gentle curve, dwell."""
    dist = haversine_m(a[0], a[1], b[0], b[1])
    step = speed_kmh / 3.6 * interval_s
    n = max(2, int(dist / step))
    pts = []
    for _ in range(int(dwell_s / interval_s)):
        pts.append((a[0], a[1], 0.0))
    for i in range(n + 1):
        f = i / n
        # ease in / out so the speed reads as a real vehicle pulling away
        v = speed_kmh * min(1.0, min(f, 1 - f) * 8 + 0.15) if 0 < f < 1 else 0.0
        lat = a[0] + (b[0] - a[0]) * f
        lon = a[1] + (b[1] - a[1]) * f
        # perpendicular bow, 3% of the leg, so the trail is not a ruler
        bow = math.sin(f * math.pi) * 0.03
        dlat, dlon = (b[0] - a[0]), (b[1] - a[1])
        lat += -dlon * bow
        lon += dlat * bow
        pts.append((lat, lon, v))
    for _ in range(int(dwell_s / interval_s)):
        pts.append((b[0], b[1], 0.0))
    return pts


def gpx_route(path, speed_hint):
    ns = {"g": "http://www.topografix.com/GPX/1/1"}
    root = ET.parse(path).getroot()
    pts = root.findall(".//g:trkpt", ns) or root.findall(".//trkpt")
    out = []
    for p in pts:
        out.append((float(p.get("lat")), float(p.get("lon")), speed_hint))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--api-url", default=os.environ.get("FINBLADE_API_URL", "http://127.0.0.1:8000"))
    ap.add_argument("--key", default=os.environ.get("FINBLADE_API_KEY"))
    ap.add_argument("--id", default="VEH-DEMO-1", help="tracker id to report as")
    ap.add_argument("--from", dest="src", help="branch id to start at")
    ap.add_argument("--to", dest="dst", help="branch id to drive to")
    ap.add_argument("--gpx", help="replay a recorded GPX track instead")
    ap.add_argument("--speed", type=float, default=80.0, help="km/h on the synthetic leg")
    ap.add_argument("--interval", type=float, default=5.0, help="simulated seconds between reports")
    ap.add_argument("--dwell", type=float, default=60.0, help="simulated seconds parked at each end")
    ap.add_argument("--time-scale", type=float, default=10.0, help="simulated seconds per wall second")
    ap.add_argument("--loop", action="store_true", help="drive back and forth until stopped")
    ap.add_argument("--register", action="store_true", help="register the tracker first (home = --from)")
    args = ap.parse_args()

    if args.gpx:
        route = gpx_route(args.gpx, args.speed)
        label = os.path.basename(args.gpx)
    else:
        if not (args.src and args.dst):
            ap.error("--from and --to (or --gpx) are required")
        coords = branch_coords(args.api_url, args.key)
        for bid in (args.src, args.dst):
            if bid not in coords:
                print(f"branch {bid} is not on the map (no lat/lon). Place it on the Map page first.")
                return 2
        a, b = coords[args.src], coords[args.dst]
        route = synth_route(a, b, args.speed, args.interval, args.dwell)
        km = haversine_m(a[0], a[1], b[0], b[1]) / 1000.0
        label = f"{a[2]} -> {b[2]} ({km:.0f} km)"
        if args.register:
            print("register:", api(args.api_url, args.key, "/api/v1/trackers",
                                   {"tracker_id": args.id, "name": f"Demo van {args.id[-1]}",
                                    "home_branch_id": args.src, "asset_label": "DEMO"}))
    wall = args.interval / max(0.1, args.time_scale)
    print(f"{args.id}: {label}, {len(route)} reports, one every {wall:.2f}s wall "
          f"({args.interval:.0f}s simulated). Ctrl-C to stop.")
    sim_t = time.time()
    try:
        while True:
            prev = None
            for lat, lon, v in route:
                hdg = bearing(prev[0], prev[1], lat, lon) if prev and v > 0 else None
                body = {"tracker_id": args.id, "lat": round(lat, 6), "lon": round(lon, 6),
                        "speed_kmh": round(v, 1), "heading": hdg, "ts": sim_t,
                        "accuracy_m": 6.0, "battery_pct": 80.0}
                try:
                    r = api(args.api_url, args.key, "/api/v1/trackers/ingest", body)
                    tag = (" at " + r["at_branch_id"]) if r.get("at_branch_id") else ""
                    ev = (" " + ",".join(r["events"])) if r.get("events") else ""
                    print(f"\r  {lat:.5f},{lon:.5f} {v:5.1f} km/h{tag}{ev:<24}", end="", flush=True)
                    if ev:
                        print()
                except urllib.error.HTTPError as e:
                    print("\n  refused:", e.code, e.read().decode(errors="replace"))
                except urllib.error.URLError as e:
                    print("\n  cannot reach the API:", e.reason)
                    time.sleep(2)
                prev = (lat, lon)
                sim_t += args.interval
                time.sleep(wall)
            print()
            if not args.loop:
                break
            route = list(reversed(route))
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
