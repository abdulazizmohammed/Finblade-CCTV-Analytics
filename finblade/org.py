"""Organisational hierarchy: Region -> City -> Branch.

The customer (Wareed Medical Laboratories, KSA) runs its network this way and
its command centre thinks in these terms: "how is the Western region doing",
"which Jeddah branches have an open alert", "show me the cameras at JED-01".
Every camera therefore belongs to exactly one branch, every branch to one city,
every city to one region, and every read that rolls counts up follows those
edges and nothing else.

THE ONE MAPPING RULE. A camera's ``site_id`` IS its branch id. ``site_id`` was
already carried by every camera, zone reading, event and alert (the workers
send it, the forwarder routes on it), so the hierarchy attaches to the data
that already exists rather than adding a second key that could disagree with
the first. A branch row whose ``branch_id`` equals a camera's ``site_id`` is
that camera's branch; a camera whose ``site_id`` matches no branch is
UNASSIGNED and is shown as such, never silently dropped or guessed into a
branch by name.

Nothing here reads a database or a socket. It takes the rows the store holds
and the live camera/zone/alert lists the API already builds, and returns the
tree and the roll-ups. Pure stdlib, unit-testable in milliseconds.
"""

import re
from typing import Dict, Iterable, List, Optional, Set

# Identifiers are keys in URLs, YAML, query strings and the ``site_id`` the
# workers post, so they are kept to a character set that survives all of those
# without quoting. Names are free text; ids are not.
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

BRANCH_TYPES = ("LAB", "COLLECTION", "HQ", "WAREHOUSE", "OTHER")
DEFAULT_BRANCH_TYPE = "LAB"

# Camera states that count as "up". Mirrors the dashboard: ONLINE plus the two
# still-trying states are online; OFFLINE and DISABLED are not.
_DOWN_STATES = ("OFFLINE", "DISABLED")
_DEGRADED_STATES = ("DEGRADED", "RECONNECTING")


def norm_id(value) -> Optional[str]:
    """A trimmed identifier, or None when it is missing or malformed."""
    if value is None:
        return None
    s = str(value).strip()
    return s if _ID_RE.match(s) else None


def _name(payload: dict, key: str, fallback: str) -> str:
    n = payload.get("name")
    n = str(n).strip() if n is not None else ""
    return n or fallback


# ---------------------------------------------------------------- validation --
# Each returns (row, errors). A row is only produced when errors is empty, so a
# caller can write it straight to the store.

def validate_region(payload: dict) -> (Optional[dict], List[str]):
    errors = []
    rid = norm_id(payload.get("region_id"))
    if not rid:
        errors.append("region_id is required: letters, digits, '_', '-' or '.'")
    if errors:
        return None, errors
    return {"region_id": rid, "name": _name(payload, "name", rid),
            "sort_order": int(payload.get("sort_order") or 0)}, []


def validate_city(payload: dict, region_ids: Iterable[str]) -> (Optional[dict], List[str]):
    errors = []
    cid = norm_id(payload.get("city_id"))
    rid = norm_id(payload.get("region_id"))
    if not cid:
        errors.append("city_id is required: letters, digits, '_', '-' or '.'")
    if not rid:
        errors.append("region_id is required")
    elif rid not in set(region_ids):
        errors.append(f"unknown region_id {rid!r}: create the region first")
    if errors:
        return None, errors
    return {"city_id": cid, "region_id": rid, "name": _name(payload, "name", cid)}, []


def validate_branch(payload: dict, city_ids: Iterable[str]) -> (Optional[dict], List[str]):
    errors = []
    bid = norm_id(payload.get("branch_id"))
    cid = norm_id(payload.get("city_id"))
    if not bid:
        errors.append("branch_id is required: letters, digits, '_', '-' or '.'")
    if not cid:
        errors.append("city_id is required")
    elif cid not in set(city_ids):
        errors.append(f"unknown city_id {cid!r}: create the city first")
    btype = str(payload.get("branch_type") or DEFAULT_BRANCH_TYPE).strip().upper()
    if btype not in BRANCH_TYPES:
        errors.append(f"branch_type must be one of {', '.join(BRANCH_TYPES)}")
    # Coordinates are optional (a branch can exist before it is placed on the
    # map) but must come as a pair, and must be a real point on Earth.
    lat, lon = payload.get("lat"), payload.get("lon")
    if (lat is None) != (lon is None):
        errors.append("lat and lon must be given together")
    elif lat is not None:
        try:
            lat, lon = float(lat), float(lon)
            if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
                errors.append("lat must be within ±90 and lon within ±180")
        except (TypeError, ValueError):
            errors.append("lat and lon must be numbers")
    fence = payload.get("geofence_m")
    if fence is not None:
        try:
            fence = float(fence)
            if not (10.0 <= fence <= 5000.0):
                errors.append("geofence_m must be between 10 and 5000 metres")
        except (TypeError, ValueError):
            errors.append("geofence_m must be a number")
    if errors:
        return None, errors
    return {"branch_id": bid, "city_id": cid, "name": _name(payload, "name", bid),
            "branch_type": btype,
            "address": (str(payload.get("address")).strip()
                        if payload.get("address") else None),
            "timezone": (str(payload.get("timezone")).strip()
                         if payload.get("timezone") else None),
            "lat": lat, "lon": lon, "geofence_m": fence}, []


# --------------------------------------------------------------------- scope --

def branches_in_scope(cities: List[dict], branches: List[dict],
                      region_id: str = None, city_id: str = None,
                      branch_id: str = None) -> Optional[Set[str]]:
    """The branch ids (= site_ids) a query is narrowed to, or None for "all".

    Filters INTERSECT rather than the narrowest winning: ``?region_id=WEST&
    branch_id=RUH-01`` is a contradiction and returns the empty set, which is
    what a contradiction should return. An unknown id likewise returns empty —
    a typo must not quietly widen to the whole network.
    """
    if not (region_id or city_id or branch_id):
        return None
    city_region = {c["city_id"]: c["region_id"] for c in cities}
    keep: Set[str] = set()
    for b in branches:
        bid, cid = b["branch_id"], b.get("city_id")
        if branch_id and bid != branch_id:
            continue
        if city_id and cid != city_id:
            continue
        if region_id and city_region.get(cid) != region_id:
            continue
        keep.add(bid)
    return keep


def in_scope(rows: List[dict], sites: Optional[Set[str]]) -> List[dict]:
    """Rows whose site_id is in scope. None means no scope was asked for."""
    if sites is None:
        return rows
    return [r for r in rows if r.get("site_id") in sites]


# ------------------------------------------------------------------- rollups --

def _empty_rollup() -> dict:
    return {"cameras": 0, "cameras_online": 0, "cameras_degraded": 0,
            "cameras_offline": 0, "people_in_view": 0,
            "zones": 0, "zones_warning": 0, "zones_critical": 0,
            "alerts_open": 0, "alerts_critical": 0, "alerts_warning": 0,
            "alerts_compliance": 0}


def _add(into: dict, more: dict) -> None:
    for k, v in more.items():
        into[k] = into.get(k, 0) + v


def _camera_rollup(cams: List[dict]) -> dict:
    r = _empty_rollup()
    for c in cams:
        st = str(c.get("effective_state") or "OFFLINE").upper()
        r["cameras"] += 1
        if st in _DOWN_STATES:
            r["cameras_offline"] += 1
        else:
            r["cameras_online"] += 1
            if st in _DEGRADED_STATES:
                r["cameras_degraded"] += 1
            # Summed over cameras, so a person seen by two cameras at once
            # counts twice. Same caveat the dashboard prints; a branch with one
            # camera per room is exact, one with overlapping views is not.
            if st == "ONLINE":
                r["people_in_view"] += int(c.get("people_in_view") or 0)
    return r


def _zone_rollup(zones: List[dict]) -> dict:
    r = {"zones": 0, "zones_warning": 0, "zones_critical": 0}
    for z in zones:
        r["zones"] += 1
        st = str(z.get("status") or "NORMAL").upper()
        if st == "WARNING":
            r["zones_warning"] += 1
        elif st == "CRITICAL":
            r["zones_critical"] += 1
    return r


def _alert_rollup(alerts: List[dict]) -> dict:
    r = {"alerts_open": 0, "alerts_critical": 0, "alerts_warning": 0,
         "alerts_compliance": 0}
    for a in alerts:
        r["alerts_open"] += 1
        sev = str(a.get("severity") or "").upper()
        if sev in ("RED", "CRITICAL"):
            r["alerts_critical"] += 1
        elif sev in ("AMBER", "WARNING"):
            r["alerts_warning"] += 1
        elif sev == "COMPLIANCE":
            r["alerts_compliance"] += 1
    return r


def _by_site(rows: Iterable[dict]) -> Dict[Optional[str], List[dict]]:
    out: Dict[Optional[str], List[dict]] = {}
    for r in rows:
        out.setdefault(r.get("site_id"), []).append(r)
    return out


def _camera_view(c: dict) -> dict:
    """What the tree carries per camera: enough to draw a status pill and link
    to it, and nothing that could hold a credential."""
    return {"camera_id": c.get("camera_id"), "name": c.get("name"),
            "effective_state": c.get("effective_state"),
            "people_in_view": int(c.get("people_in_view") or 0),
            "site_id": c.get("site_id")}


def build_tree(regions: List[dict], cities: List[dict], branches: List[dict],
               cameras: List[dict] = (), zones: List[dict] = (),
               alerts: List[dict] = ()) -> dict:
    """The nested Region -> City -> Branch tree with counts rolled up.

    Every level carries a ``rollup`` that is the SUM of its children, so the
    network total, a region and a branch all answer "how many cameras are
    online" the same way. A camera whose site_id matches no branch lands in
    ``unassigned`` (grouped by the site_id it claimed), and its counts are in
    the network total but in no region — the total must still be the truth
    about the whole deployment.
    """
    cams_by = _by_site(cameras)
    zones_by = _by_site(zones)
    alerts_by = _by_site(alerts)
    known = {b["branch_id"] for b in branches}

    def branch_node(b: dict) -> dict:
        bid = b["branch_id"]
        roll = _camera_rollup(cams_by.get(bid, []))
        _add(roll, _zone_rollup(zones_by.get(bid, [])))
        _add(roll, _alert_rollup(alerts_by.get(bid, [])))
        return {"branch_id": bid, "name": b.get("name") or bid,
                "branch_type": b.get("branch_type") or DEFAULT_BRANCH_TYPE,
                "city_id": b.get("city_id"), "address": b.get("address"),
                "timezone": b.get("timezone"),
                "lat": b.get("lat"), "lon": b.get("lon"),
                "geofence_m": b.get("geofence_m"),
                "cameras": sorted((_camera_view(c) for c in cams_by.get(bid, [])),
                                  key=lambda c: str(c["camera_id"])),
                "rollup": roll}

    by_city: Dict[str, List[dict]] = {}
    for b in sorted(branches, key=lambda x: (str(x.get("name") or ""), x["branch_id"])):
        by_city.setdefault(b.get("city_id"), []).append(branch_node(b))

    by_region: Dict[str, List[dict]] = {}
    for c in sorted(cities, key=lambda x: (str(x.get("name") or ""), x["city_id"])):
        kids = by_city.get(c["city_id"], [])
        roll = _empty_rollup()
        for k in kids:
            _add(roll, k["rollup"])
        by_region.setdefault(c.get("region_id"), []).append(
            {"city_id": c["city_id"], "name": c.get("name") or c["city_id"],
             "region_id": c.get("region_id"), "branches": kids, "rollup": roll})

    total = _empty_rollup()
    region_nodes = []
    for r in sorted(regions, key=lambda x: (int(x.get("sort_order") or 0),
                                            str(x.get("name") or ""), x["region_id"])):
        kids = by_region.get(r["region_id"], [])
        roll = _empty_rollup()
        for k in kids:
            _add(roll, k["rollup"])
        _add(total, roll)
        region_nodes.append({"region_id": r["region_id"],
                             "name": r.get("name") or r["region_id"],
                             "cities": kids, "rollup": roll})

    unassigned = []
    for site, cams in sorted(cams_by.items(), key=lambda kv: str(kv[0] or "")):
        if site in known:
            continue
        roll = _camera_rollup(cams)
        _add(roll, _zone_rollup(zones_by.get(site, [])))
        _add(roll, _alert_rollup(alerts_by.get(site, [])))
        _add(total, roll)
        unassigned.append({"site_id": site,
                           "cameras": sorted((_camera_view(c) for c in cams),
                                             key=lambda c: str(c["camera_id"])),
                           "rollup": roll})

    return {"regions": region_nodes, "unassigned": unassigned, "rollup": total,
            "counts": {"regions": len(regions), "cities": len(cities),
                       "branches": len(branches),
                       "cameras_unassigned": sum(len(u["cameras"]) for u in unassigned)}}


def flatten_import(payload: dict) -> (List[dict], List[dict], List[dict], List[str]):
    """Turn a nested import document into (regions, cities, branches, errors).

    The document mirrors the tree: regions carry cities, cities carry branches.
    Validation is structural and referential; nothing is written here.
    """
    regions, cities, branches, errors = [], [], [], []
    for i, r in enumerate(payload.get("regions") or []):
        row, errs = validate_region(dict(r, sort_order=r.get("sort_order", i)))
        if errs:
            errors.extend(f"regions[{i}]: {e}" for e in errs)
            continue
        regions.append(row)
        for j, c in enumerate(r.get("cities") or []):
            crow, errs = validate_city(dict(c, region_id=row["region_id"]),
                                       [row["region_id"]])
            if errs:
                errors.extend(f"regions[{i}].cities[{j}]: {e}" for e in errs)
                continue
            cities.append(crow)
            for k, b in enumerate(c.get("branches") or []):
                brow, errs = validate_branch(dict(b, city_id=crow["city_id"]),
                                             [crow["city_id"]])
                if errs:
                    errors.extend(f"regions[{i}].cities[{j}].branches[{k}]: {e}"
                                  for e in errs)
                    continue
                branches.append(brow)
    for label, rows, key in (("region", regions, "region_id"),
                             ("city", cities, "city_id"),
                             ("branch", branches, "branch_id")):
        seen = set()
        for row in rows:
            if row[key] in seen:
                errors.append(f"duplicate {label} id {row[key]!r}")
            seen.add(row[key])
    return regions, cities, branches, errors
