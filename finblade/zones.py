"""Zone model + point-to-zone assignment.

A zone is a polygon plus capacity/area/restricted metadata loaded from
config/cameras.yaml. Assignment uses the foot point; restricted zones win ties
so an intrusion is never masked by an overlapping normal zone.
"""

from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from .geometry import Point, point_in_polygon


@dataclass
class Zone:
    zone_id: str
    zone_name: str
    restricted: bool
    capacity_max: int
    area_sqm: float
    polygon: List[Tuple[float, float]] = field(default_factory=list)

    def contains(self, point: Point) -> bool:
        return point_in_polygon(point, self.polygon)


def zone_from_dict(d: dict) -> Zone:
    return Zone(
        zone_id=d["zone_id"],
        zone_name=d["zone_name"],
        restricted=bool(d.get("restricted", False)),
        capacity_max=int(d.get("capacity_max", 0)),
        area_sqm=float(d.get("area_sqm", 0.0)),
        polygon=[(float(x), float(y)) for x, y in d["polygon"]],
    )


def zone_of(point: Point, zones: Sequence[Zone]) -> Optional[str]:
    """Return the zone_id containing ``point``; restricted zones take priority.

    ``sorted(..., key=lambda z: not z.restricted)`` places restricted (True ->
    False) first, so if a point falls inside both a restricted and a normal zone
    the restricted one wins.
    """
    for z in sorted(zones, key=lambda z: not z.restricted):
        if z.contains(point):
            return z.zone_id
    return None
