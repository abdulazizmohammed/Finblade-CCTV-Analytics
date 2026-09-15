"""GPS tracking: device dialects, distance, branch geofences, silence.

A tracker is a phone running Traccar Client, or a 4G tracker unit, fitted to
a lab vehicle carrying samples or a device. It reports its position over
plain HTTP every few seconds. This module is everything about those reports
that does not touch a socket or a database:

  * parse_osmand()  — the "OsmAnd" query-string dialect Traccar Client and
                      most modern trackers speak:  ?id=..&lat=..&lon=..&...
  * parse_gprmc()   — the OpenGTS dialect: an NMEA $GPRMC sentence in a
                      query string. Dormant server, live install base.
  * haversine_m()   — metres between two WGS84 points.
  * GeofenceEngine  — arrival / departure at a branch, with hysteresis and
                      a confirm count so a vehicle idling at the gate does
                      not flap. Same discipline as the zone debounce.
  * silent_for()    — how long since a tracker last reported.

A TRACKER IS A VEHICLE OR AN ASSET, NEVER A PERSON. Nothing here accepts,
stores or derives a driver identity; the OsmAnd `driverUniqueId` field is
dropped on parse. Positions are telemetry about a thing.

Pure stdlib. Unit-testable in milliseconds.
"""

import math
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")

KNOT_KMH = 1.852

# Geofence defaults. 150 m covers a lab's car park without spilling onto
# the road past it; the exit radius is wider so a vehicle parked right on
# the boundary does not arrive and depart with every GPS wobble. Two
# consecutive readings confirm a transition: at a 10 s report interval that
# is 20 s, which no drive-past survives and every real stop does.
DEFAULT_GEOFENCE_M = 150.0
EXIT_FACTOR = 1.5
CONFIRM_COUNT = 2

# A phone that stops reporting for this long is offline. Generous next to the
# camera's 30 s because a vehicle drives through tunnels and dead spots, and
# Traccar Client backs off when the phone is stationary.
DEFAULT_SILENT_S = 300.0


def norm_tracker_id(value) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    return s if _ID_RE.match(s) else None


# ------------------------------------------------------------------ parsing --

@dataclass
class Position:
    tracker_id: str
    ts: float
    lat: float
    lon: float
    speed_kmh: Optional[float] = None
    heading: Optional[float] = None
    altitude_m: Optional[float] = None
    accuracy_m: Optional[float] = None
    battery_pct: Optional[float] = None
    dialect: str = "json"

    def to_dict(self) -> dict:
        return {"tracker_id": self.tracker_id, "ts": self.ts, "lat": self.lat,
                "lon": self.lon, "speed_kmh": self.speed_kmh,
                "heading": self.heading, "altitude_m": self.altitude_m,
                "accuracy_m": self.accuracy_m, "battery_pct": self.battery_pct,
                "dialect": self.dialect}


def _f(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _check_point(lat, lon, errors: List[str]) -> Tuple[Optional[float], Optional[float]]:
    lat, lon = _f(lat), _f(lon)
    if lat is None or lon is None:
        errors.append("lat and lon are required numbers")
        return None, None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        errors.append("lat/lon out of range")
    # 0,0 is the classic "GPS not fixed yet" value from a cold tracker. It is
    # in the Gulf of Guinea, not in Riyadh, and plotting it would swing the
    # map to Africa. Reject it as a non-fix.
    if lat == 0.0 and lon == 0.0:
        errors.append("0,0 is not a fix")
    return lat, lon


def _ts(v, now: float) -> float:
    """Device timestamp: epoch seconds, or milliseconds, or absent (= now).

    OsmAnd sends seconds; some firmware sends milliseconds. Anything above
    1e11 cannot be seconds (that is the year 5138) so it is treated as ms.
    """
    t = _f(v)
    if t is None or t <= 0:
        return now
    if t > 1e11:
        t /= 1000.0
    return t


def parse_osmand(params: dict, now: Optional[float] = None) -> Tuple[Optional[Position], List[str]]:
    """The Traccar-Client / OsmAnd query dialect.

    id, lat, lon required; timestamp (epoch s), speed (KNOTS — that is what
    Traccar Client sends), bearing, altitude, accuracy, batt optional.
    `driverUniqueId` and anything else unknown is dropped.
    """
    now = time.time() if now is None else now
    errors: List[str] = []
    tid = norm_tracker_id(params.get("id") or params.get("deviceid"))
    if not tid:
        errors.append("id is required: letters, digits, '_', '-', '.', ':'")
    lat, lon = _check_point(params.get("lat"), params.get("lon"), errors)
    if errors:
        return None, errors
    speed = _f(params.get("speed"))
    return Position(
        tracker_id=tid, ts=_ts(params.get("timestamp"), now), lat=lat, lon=lon,
        speed_kmh=(round(speed * KNOT_KMH, 1) if speed is not None else None),
        heading=_f(params.get("bearing") or params.get("heading")),
        altitude_m=_f(params.get("altitude")),
        accuracy_m=_f(params.get("accuracy")),
        battery_pct=_f(params.get("batt") or params.get("battery")),
        dialect="osmand"), []


def _nmea_deg(value: str, hemi: str) -> Optional[float]:
    """ddmm.mmmm / dddmm.mmmm with N/S/E/W to signed decimal degrees."""
    v = _f(value)
    if v is None:
        return None
    deg = int(v // 100)
    minutes = v - deg * 100
    out = deg + minutes / 60.0
    return -out if hemi in ("S", "W") else out


def parse_gprmc(params: dict, now: Optional[float] = None) -> Tuple[Optional[Position], List[str]]:
    """The OpenGTS "gprmc" HTTP dialect: ?dev=<id>&gprmc=$GPRMC,....

    $GPRMC,hhmmss,A,ddmm.mmmm,N,dddmm.mmmm,E,speed_knots,course,ddmmyy,...
    Field 2 is the fix status; 'V' means no fix and is rejected. OpenGTS also
    accepts `id` and `acct`; acct (the OpenGTS account) is ignored — the
    tenant is implicit here.
    """
    now = time.time() if now is None else now
    errors: List[str] = []
    tid = norm_tracker_id(params.get("dev") or params.get("id"))
    if not tid:
        errors.append("dev (or id) is required")
    sentence = str(params.get("gprmc") or "").strip()
    if not sentence.startswith("$GPRMC"):
        errors.append("gprmc must be a $GPRMC sentence")
        return None, errors
    parts = sentence.split("*")[0].split(",")
    if len(parts) < 10:
        errors.append("gprmc sentence is truncated")
        return None, errors
    if parts[2].upper() != "A":
        errors.append("gprmc reports no fix (status V)")
        return None, errors
    lat = _nmea_deg(parts[3], parts[4].upper())
    lon = _nmea_deg(parts[5], parts[6].upper())
    lat, lon = _check_point(lat, lon, errors)
    if errors:
        return None, errors
    ts = now
    try:
        hh, mm, ss = int(parts[1][0:2]), int(parts[1][2:4]), float(parts[1][4:])
        dd, mo, yy = int(parts[9][0:2]), int(parts[9][2:4]), 2000 + int(parts[9][4:6])
        import calendar
        ts = calendar.timegm((yy, mo, dd, hh, mm, 0)) + ss
    except (ValueError, IndexError):
        pass
    speed = _f(parts[7])
    return Position(
        tracker_id=tid, ts=ts, lat=lat, lon=lon,
        speed_kmh=(round(speed * KNOT_KMH, 1) if speed is not None else None),
        heading=_f(parts[8]), dialect="gprmc"), []


def parse_json(body: dict, now: Optional[float] = None) -> Tuple[Optional[Position], List[str]]:
    """Our own dialect, for the phone web page and the replay script.
    Speed is km/h here, not knots."""
    now = time.time() if now is None else now
    errors: List[str] = []
    if not isinstance(body, dict):
        return None, ["object expected"]
    tid = norm_tracker_id(body.get("tracker_id") or body.get("id"))
    if not tid:
        errors.append("tracker_id is required")
    lat, lon = _check_point(body.get("lat"), body.get("lon"), errors)
    if errors:
        return None, errors
    return Position(
        tracker_id=tid, ts=_ts(body.get("ts") or body.get("timestamp"), now),
        lat=lat, lon=lon, speed_kmh=_f(body.get("speed_kmh")),
        heading=_f(body.get("heading")), altitude_m=_f(body.get("altitude_m")),
        accuracy_m=_f(body.get("accuracy_m")),
        battery_pct=_f(body.get("battery_pct")), dialect="json"), []


# ----------------------------------------------------------------- geometry --

def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# ---------------------------------------------------------------- geofences --

@dataclass
class Fence:
    branch_id: str
    lat: float
    lon: float
    radius_m: float = DEFAULT_GEOFENCE_M


@dataclass
class Transition:
    kind: str                # "ARRIVED" | "DEPARTED"
    tracker_id: str
    branch_id: str
    ts: float
    distance_m: float
    dwell_s: Optional[float] = None    # on DEPARTED: how long it was there


@dataclass
class _State:
    at: Optional[str] = None           # confirmed branch
    at_since: Optional[float] = None
    pending: Optional[str] = None      # candidate branch (or "" for outside)
    pending_n: int = 0


class GeofenceEngine:
    """Which branch a tracker is at, with hysteresis and a confirm count.

    Inside = within radius. Outside = beyond radius * EXIT_FACTOR. Between
    the two is a dead band where nothing changes. A candidate state must be
    seen CONFIRM_COUNT times in a row before it is accepted, so one bad fix
    never produces an arrival, and a vehicle parked on the boundary does
    not produce a stream of them.

    Nearest fence wins when two overlap (two collection points on one
    street); a tracker is at one branch at a time.
    """

    def __init__(self, fences: List[Fence] = (), confirm: int = CONFIRM_COUNT,
                 exit_factor: float = EXIT_FACTOR):
        self._fences: Dict[str, Fence] = {f.branch_id: f for f in fences}
        self._state: Dict[str, _State] = {}
        self.confirm = max(1, int(confirm))
        self.exit_factor = float(exit_factor)

    def set_fences(self, fences: List[Fence]) -> None:
        self._fences = {f.branch_id: f for f in fences}

    def restore(self, tracker_id: str, at: Optional[str], at_since: Optional[float]) -> None:
        """Seed state from tracker_live after a restart, so a vehicle already
        parked at a branch does not re-arrive on the first report."""
        self._state[tracker_id] = _State(at=at, at_since=at_since)

    def at(self, tracker_id: str) -> Tuple[Optional[str], Optional[float]]:
        s = self._state.get(tracker_id)
        return (s.at, s.at_since) if s else (None, None)

    def nearest(self, lat: float, lon: float) -> Tuple[Optional[Fence], float]:
        best, best_d = None, float("inf")
        for f in self._fences.values():
            d = haversine_m(lat, lon, f.lat, f.lon)
            if d < best_d:
                best, best_d = f, d
        return best, best_d

    def observe(self, p: Position) -> List[Transition]:
        s = self._state.setdefault(p.tracker_id, _State())
        fence, dist = self.nearest(p.lat, p.lon)
        # Candidate: the nearest fence if inside it; otherwise "outside" only
        # if we are clearly beyond the exit radius of where we ARE.
        if fence is not None and dist <= fence.radius_m:
            candidate = fence.branch_id
        elif s.at is not None:
            cur = self._fences.get(s.at)
            if cur is None:
                candidate = ""           # fence deleted under us: we are out
            else:
                d_cur = haversine_m(p.lat, p.lon, cur.lat, cur.lon)
                candidate = "" if d_cur > cur.radius_m * self.exit_factor else s.at
        else:
            candidate = ""
        if candidate == (s.at or ""):
            s.pending, s.pending_n = None, 0
            return []
        if candidate != s.pending:
            s.pending, s.pending_n = candidate, 1
        else:
            s.pending_n += 1
        if s.pending_n < self.confirm:
            return []
        out: List[Transition] = []
        if s.at is not None:
            out.append(Transition("DEPARTED", p.tracker_id, s.at, p.ts, dist,
                                  dwell_s=(p.ts - s.at_since) if s.at_since else None))
        if candidate:
            out.append(Transition("ARRIVED", p.tracker_id, candidate, p.ts, dist))
            s.at, s.at_since = candidate, p.ts
        else:
            s.at, s.at_since = None, None
        s.pending, s.pending_n = None, 0
        return out


# ------------------------------------------------------------------ silence --

def silent_for(last_ts: Optional[float], now: Optional[float] = None) -> Optional[float]:
    if last_ts is None:
        return None
    return max(0.0, (time.time() if now is None else now) - float(last_ts))


def fences_from_branches(branches: List[dict], default_radius: float = DEFAULT_GEOFENCE_M) -> List[Fence]:
    """Only branches that have been placed on the map get a fence."""
    out = []
    for b in branches:
        if b.get("lat") is None or b.get("lon") is None:
            continue
        out.append(Fence(b["branch_id"], float(b["lat"]), float(b["lon"]),
                         float(b.get("geofence_m") or default_radius)))
    return out
