"""Zone model + point-to-zone assignment.

A zone is a polygon plus capacity/area/type/threshold metadata. Assignment uses
the foot point; restricted zones win ties so an intrusion is never masked by an
overlapping normal zone.

Polygons are held at runtime in PIXEL coordinates (they are tested against pixel
foot points). For portability they can also be stored/exported NORMALIZED
(x/frame_width, y/frame_height) so a zone survives a resolution change.

Backward compatible: every new field has a default, and ``restricted`` (bool) and
``zone_type`` are derived from each other so old pixel-polygon configs still load.
"""

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from .geometry import Point, point_in_polygon

# Zone types (Req 5). RESTRICTED drives the no-go behaviour; the rest are metadata
# used by later analytics (e.g. ENTRANCE/EXIT bias inflow/outflow).
ZONE_TYPES = {"MONITORED", "RESTRICTED", "ENTRANCE", "EXIT", "TRANSITION", "UNMONITORED"}

# An UNMONITORED zone is a DETECTION MASK, not just a zone that reports nothing.
# Any detection whose foot point lands inside one is discarded outright.
#
# This exists because a detector cannot tell a person from a picture of one. A
# figure in a mirror, a TV showing people, a printed poster, or a window onto a
# neighbouring space are all genuinely person-shaped, and YOLO is right to fire
# on them. They then inflate occupancy and, worse, enter the ReID gallery as
# permanent phantom identities that other people can match against.
#
# The only reliable fix is human knowledge of which parts of the frame are not
# real floor — which is exactly what drawing a polygon expresses.
IGNORED_ZONE_TYPE = "UNMONITORED"


@dataclass
class Zone:
    zone_id: str
    zone_name: str
    restricted: bool
    capacity_max: int
    area_sqm: float
    polygon: List[Tuple[float, float]] = field(default_factory=list)
    camera_id: Optional[str] = None
    zone_type: str = "MONITORED"
    warning_density: float = 2.0
    critical_density: float = 4.0
    loitering_threshold_sec: float = 30.0
    adjacency_list: List[str] = field(default_factory=list)
    colour: Optional[str] = None
    enabled: bool = True

    def contains(self, point: Point) -> bool:
        return point_in_polygon(point, self.polygon)

    def normalized_polygon(self, frame_width: float, frame_height: float):
        if not (frame_width and frame_height):
            return []
        return [(x / frame_width, y / frame_height) for x, y in self.polygon]

    def to_dict(self, frame_width: float = None, frame_height: float = None) -> dict:
        d = {
            "zone_id": self.zone_id,
            "zone_name": self.zone_name,
            "camera_id": self.camera_id,
            "zone_type": self.zone_type,
            "restricted": self.restricted,
            "capacity_max": self.capacity_max,
            "area_sqm": self.area_sqm,
            "warning_density": self.warning_density,
            "critical_density": self.critical_density,
            "loitering_threshold_sec": self.loitering_threshold_sec,
            "adjacency_list": list(self.adjacency_list),
            "colour": self.colour,
            "enabled": self.enabled,
            "polygon": [[x, y] for x, y in self.polygon],
        }
        if frame_width and frame_height:
            d["normalized_polygon"] = [[nx, ny]
                                       for nx, ny in self.normalized_polygon(frame_width, frame_height)]
        return d


def zone_from_dict(d: dict, frame_width: float = None, frame_height: float = None) -> Zone:
    # Derive zone_type <-> restricted for backward compatibility.
    zt = d.get("zone_type")
    restricted = d.get("restricted")
    if zt is None:
        zt = "RESTRICTED" if restricted else "MONITORED"
    zt = str(zt).upper()
    if zt not in ZONE_TYPES:
        zt = "MONITORED"
    if restricted is None:
        restricted = (zt == "RESTRICTED")
    restricted = bool(restricted) or zt == "RESTRICTED"

    # Polygon: prefer explicit pixel coords; else convert normalized using frame size.
    if d.get("polygon"):
        polygon = [(float(x), float(y)) for x, y in d["polygon"]]
    elif d.get("normalized_polygon") and frame_width and frame_height:
        polygon = [(float(nx) * frame_width, float(ny) * frame_height)
                   for nx, ny in d["normalized_polygon"]]
    else:
        polygon = []

    return Zone(
        zone_id=d["zone_id"],
        zone_name=d.get("zone_name", d["zone_id"]),
        restricted=restricted,
        capacity_max=int(d.get("capacity_max", 0)),
        area_sqm=float(d.get("area_sqm", 0.0)),
        polygon=polygon,
        camera_id=d.get("camera_id"),
        zone_type=zt,
        warning_density=float(d.get("warning_density", 2.0)),
        critical_density=float(d.get("critical_density", 4.0)),
        loitering_threshold_sec=float(d.get("loitering_threshold_sec", 30.0)),
        adjacency_list=list(d.get("adjacency_list", []) or []),
        colour=d.get("colour"),
        enabled=bool(d.get("enabled", True)),
    )


def in_ignored_region(point: Point, zones) -> bool:
    """True if ``point`` falls inside an UNMONITORED zone (a detection mask).

    Callers should test this BEFORE any other per-detection work and skip the
    detection entirely — not merely leave it out of occupancy. A reflection that
    still gets tracked and embedded pollutes the identity gallery even if no
    zone counts it.
    """
    for z in zones:
        if not getattr(z, "enabled", True):
            continue
        if getattr(z, "zone_type", "") == IGNORED_ZONE_TYPE and z.contains(point):
            return True
    return False


def zone_of(point: Point, zones) -> Optional[str]:
    """Return the zone_id containing ``point``; restricted zones take priority.

    Disabled zones are ignored. ``sorted(..., key=lambda z: not z.restricted)``
    places restricted (True -> False) first, so if a point falls inside both a
    restricted and a normal zone the restricted one wins.
    """
    for z in sorted(zones, key=lambda z: not z.restricted):
        if getattr(z, "enabled", True) and z.contains(point):
            return z.zone_id
    return None
