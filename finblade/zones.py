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
# DOOR is a doorway walked BOTH ways. ENTRANCE and EXIT each declare a
# direction, so they are decidable the moment someone steps into them; DOOR
# declares only that the polygon is a boundary, and the direction comes from
# the zones either side of the crossing (see finblade/presence.py).
#
# OUTSIDE is floor drawn BEYOND the facility boundary — a forecourt, a car park,
# a street. It has to be declarable because otherwise it is indistinguishable
# from interior floor, and a person stepping out of a door onto it would be read
# as walking in. Occupancy still counts it; the facility roster does not.
ZONE_TYPES = {"MONITORED", "RESTRICTED", "ENTRANCE", "EXIT", "DOOR",
              "OUTSIDE", "TRANSITION", "UNMONITORED"}

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
    # The real-world place this polygon looks at (see finblade/areas.py).
    #
    # A polygon is one camera's VIEW of somewhere; two cameras watching one
    # office draw two polygons of the same room. Pointing both at the same
    # physical_area_id is what lets occupancy be counted as distinct people
    # rather than summed per camera, which would count the overlap twice.
    #
    # None is the ordinary single-camera case and changes nothing.
    physical_area_id: Optional[str] = None
    zone_type: str = "MONITORED"
    warning_density: float = 2.0
    critical_density: float = 4.0
    loitering_threshold_sec: float = 30.0
    adjacency_list: List[str] = field(default_factory=list)
    colour: Optional[str] = None
    enabled: bool = True
    # Zones a person may arrive FROM. Declaring any makes the reverse of each a
    # wrong-way violation; leaving it empty leaves the zone unpoliced, which is
    # right for the ordinary two-way spaces that make up most of a building.
    allowed_from: List[str] = field(default_factory=list)
    # Alert above this many PEOPLE, independent of area or capacity. 0 disables.
    # Density and capacity rules need a measured area or a configured maximum
    # before they mean anything; a head count does not, which is why a small
    # restricted space is usually best policed this way.
    occupancy_threshold: int = 0
    # Distinct people crossing INTO this zone within group_window_s. 0 disables.
    group_threshold: int = 0
    group_window_s: float = 3.0
    # Run cross-camera re-identification on people in this zone. If NO zone on a
    # camera sets it, ReID runs for everyone as before; once any zone does, it
    # runs only in those zones. Matching every person across a whole site is
    # expensive and, in a uniformed environment, the case where appearance
    # matching is least reliable — so it belongs where it is operationally
    # needed: controlled entrances, restricted corridors, security areas.
    reid: bool = False

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
            "physical_area_id": self.physical_area_id,
            "zone_type": self.zone_type,
            "restricted": self.restricted,
            "capacity_max": self.capacity_max,
            "area_sqm": self.area_sqm,
            "warning_density": self.warning_density,
            "critical_density": self.critical_density,
            "loitering_threshold_sec": self.loitering_threshold_sec,
            "adjacency_list": list(self.adjacency_list),
            "allowed_from": list(self.allowed_from),
            "occupancy_threshold": self.occupancy_threshold,
            "group_threshold": self.group_threshold,
            "group_window_s": self.group_window_s,
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
        physical_area_id=(d.get("physical_area_id") or None),
        zone_type=zt,
        warning_density=float(d.get("warning_density", 2.0)),
        critical_density=float(d.get("critical_density", 4.0)),
        loitering_threshold_sec=float(d.get("loitering_threshold_sec", 30.0)),
        adjacency_list=list(d.get("adjacency_list", []) or []),
        allowed_from=[str(z) for z in (d.get("allowed_from") or []) if z],
        occupancy_threshold=int(d.get("occupancy_threshold", 0) or 0),
        group_threshold=int(d.get("group_threshold", 0) or 0),
        group_window_s=float(d.get("group_window_s", 3.0) or 3.0),
        reid=bool(d.get("reid", False)),
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
