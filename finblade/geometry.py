"""Geometry primitives: foot point + point-in-polygon.

Pure Python (no cv2) so it is testable headless. Semantics match OpenCV's
``pointPolygonTest(..., measureDist=False) >= 0``: a point exactly on an edge or
vertex counts as INSIDE. That matters for zone assignment at boundaries.
"""

from typing import Sequence, Tuple

Point = Tuple[float, float]
Polygon = Sequence[Point]

_EPS = 1e-9


def foot_point(x1: float, y1: float, x2: float, y2: float) -> Point:
    """Ground-contact point = bottom-centre of the bbox.

    Zones are assigned on the feet, not the centroid, so a tall person leaning
    over a boundary line is placed by where they actually stand.
    """
    return ((x1 + x2) / 2.0, float(max(y1, y2)))


def _on_segment(p: Point, a: Point, b: Point) -> bool:
    """True if point p lies on the closed segment a-b (collinear + in bounds)."""
    px, py = p
    ax, ay = a
    bx, by = b
    cross = (bx - ax) * (py - ay) - (by - ay) * (px - ax)
    if abs(cross) > _EPS:
        return False
    if px < min(ax, bx) - _EPS or px > max(ax, bx) + _EPS:
        return False
    if py < min(ay, by) - _EPS or py > max(ay, by) + _EPS:
        return False
    return True


def point_in_polygon(point: Point, polygon: Polygon) -> bool:
    """Ray-casting point-in-polygon with on-edge treated as inside.

    Works for any simple polygon (convex or concave), vertices in either winding.
    """
    n = len(polygon)
    if n < 3:
        return False

    # On-edge / on-vertex counts as inside (matches cv2 >= 0 semantics).
    for i in range(n):
        if _on_segment(point, polygon[i], polygon[(i + 1) % n]):
            return True

    x, y = point
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        # Does the horizontal ray at y cross edge (i, j)?
        if (yi > y) != (yj > y):
            x_cross = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < x_cross:
                inside = not inside
        j = i
    return inside
