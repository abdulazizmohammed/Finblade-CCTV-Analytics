"""Geometry: the OpenCV equivalence claim, and the cases ray-casting gets wrong.

finblade/geometry.py states that point_in_polygon matches
``cv2.pointPolygonTest(..., measureDist=False) >= 0``. Nothing tested that, and
it is not a cosmetic claim: `services/inference/main.py` assigns zones with
cv2's implementation while everything downstream of `finblade/zones.py` uses
this one. If they disagree, two code paths put the same person in different
zones and every count built on top inherits the split.

Kept separate from test_geometry.py so that file stays dependency-free —
finblade/ is meant to be testable with no cv2 and no numpy. This one skips
itself if either is missing.

Ray-casting has three classic failure modes, all of which produce a wrong answer
rather than an error: a ray passing exactly through a vertex (counted twice),
a horizontal edge lying on the ray, and reversed winding. Each is tested below
against a polygon shaped to trigger it.
"""

import random
import unittest

try:
    import cv2
    import numpy as np
    HAVE_CV2 = True
except Exception:                                  # noqa: BLE001
    HAVE_CV2 = False

from finblade.geometry import foot_point, point_in_polygon

SQUARE = [(0, 0), (10, 0), (10, 10), (0, 10)]

# A star, to get concavity and several ray crossings on one horizontal line.
STAR = [(50, 0), (61, 35), (98, 35), (68, 57),
        (79, 91), (50, 70), (21, 91), (32, 57), (2, 35), (39, 35)]

# A plus/cross — the shape where a horizontal ray through the waist crosses
# four edges, and where the notches sit outside a convex hull.
PLUS = [(3, 0), (7, 0), (7, 3), (10, 3), (10, 7), (7, 7),
        (7, 10), (3, 10), (3, 7), (0, 7), (0, 3), (3, 3)]


class TestWinding(unittest.TestCase):
    """The docstring promises either winding works. It was never checked."""

    def test_clockwise_and_counter_clockwise_agree(self):
        cw = SQUARE
        ccw = list(reversed(SQUARE))
        for p in [(5, 5), (1, 9), (15, 5), (-1, 5), (0, 0), (10, 10)]:
            self.assertEqual(point_in_polygon(p, cw), point_in_polygon(p, ccw),
                             f"winding changed the answer for {p}")

    def test_star_agrees_in_both_windings(self):
        rev = list(reversed(STAR))
        for p in [(50, 40), (50, 5), (50, 80), (10, 20), (90, 20), (50, 60)]:
            self.assertEqual(point_in_polygon(p, STAR), point_in_polygon(p, rev),
                             f"winding changed the answer for {p}")


class TestRayThroughVertex(unittest.TestCase):
    """A ray that hits a vertex must not count that vertex twice."""

    def test_ray_level_with_a_vertex_outside(self):
        # y=0 passes through two vertices of SQUARE. A point to the left is out.
        self.assertFalse(point_in_polygon((-5, 0), SQUARE))

    def test_ray_level_with_the_star_waist(self):
        # y=35 is the height of four STAR vertices at once.
        self.assertTrue(point_in_polygon((50, 35), STAR))
        self.assertFalse(point_in_polygon((-5, 35), STAR))
        self.assertFalse(point_in_polygon((120, 35), STAR))

    def test_horizontal_edge_on_the_ray(self):
        # y=3 lies along two horizontal edges of PLUS.
        self.assertTrue(point_in_polygon((5, 3), PLUS))
        self.assertFalse(point_in_polygon((-1, 3), PLUS))

    def test_notch_of_a_plus_is_outside(self):
        for corner in [(1, 1), (9, 1), (1, 9), (9, 9)]:
            self.assertFalse(point_in_polygon(corner, PLUS), corner)


class TestFootPointEdges(unittest.TestCase):
    def test_float_bbox(self):
        self.assertEqual(foot_point(10.5, 20.25, 30.5, 80.75), (20.5, 80.75))

    def test_zero_area_box(self):
        # A degenerate detection must still produce a usable point, not divide
        # by zero or raise — the vision loop feeds whatever the detector emits.
        self.assertEqual(foot_point(5, 5, 5, 5), (5.0, 5.0))

    def test_negative_coordinates(self):
        # A box clipped at the frame edge can come back with negative x.
        self.assertEqual(foot_point(-10, -20, 10, -5), (0.0, -5.0))

    def test_result_is_always_float(self):
        fx, fy = foot_point(1, 2, 3, 4)
        self.assertIsInstance(fx, float)
        self.assertIsInstance(fy, float)

    def test_x_is_the_midpoint_not_the_left_edge(self):
        fx, _ = foot_point(100, 0, 200, 50)
        self.assertEqual(fx, 150.0)


@unittest.skipUnless(HAVE_CV2, "cv2/numpy not available")
class TestMatchesOpenCV(unittest.TestCase):
    """Differential test against the implementation the docstring cites.

    Boundary points are excluded by distance rather than by guesswork: cv2's
    measureDist=True gives the signed distance to the edge, so anything closer
    than a small epsilon is skipped. Those cases are covered exactly, on integer
    coordinates, by test_on_edge_is_inside in test_geometry.py — comparing them
    here in floating point would test float32 rounding, not our semantics.
    """

    def _agree(self, polygon, points, eps=1e-6):
        contour = np.array(polygon, dtype=np.float32).reshape((-1, 1, 2))
        checked = 0
        for p in points:
            pt = (float(p[0]), float(p[1]))
            dist = cv2.pointPolygonTest(contour, pt, True)
            if abs(dist) < eps:
                continue                      # on the boundary; see docstring
            expected = dist > 0
            self.assertEqual(
                point_in_polygon(pt, polygon), expected,
                f"disagreed with cv2 at {pt} (cv2 distance {dist:.6f})")
            checked += 1
        return checked

    def test_square_grid(self):
        pts = [(x * 0.5, y * 0.5) for x in range(-4, 25) for y in range(-4, 25)]
        self.assertGreater(self._agree(SQUARE, pts), 500)

    def test_star_grid(self):
        pts = [(x, y) for x in range(-10, 111, 3) for y in range(-10, 101, 3)]
        self.assertGreater(self._agree(STAR, pts), 500)

    def test_plus_grid(self):
        pts = [(x * 0.5, y * 0.5) for x in range(-4, 25) for y in range(-4, 25)]
        self.assertGreater(self._agree(PLUS, pts), 500)

    def test_random_polygons(self):
        # Fixed seed: a differential test that fails only sometimes is worse
        # than no test, because nobody can reproduce the failure.
        rng = random.Random(20260901)
        total = 0
        for _ in range(30):
            n = rng.randint(3, 9)
            # Vertices ordered by angle, so the polygon is simple (non
            # self-intersecting) — which is what point_in_polygon documents
            # itself as supporting.
            cx, cy = rng.uniform(20, 80), rng.uniform(20, 80)
            angles = sorted(rng.uniform(0, 6.28318) for _ in range(n))
            poly = [(cx + rng.uniform(10, 40) * __import__("math").cos(a),
                     cy + rng.uniform(10, 40) * __import__("math").sin(a))
                    for a in angles]
            pts = [(rng.uniform(-20, 120), rng.uniform(-20, 120))
                   for _ in range(60)]
            total += self._agree(poly, pts)
        self.assertGreater(total, 1000)

    def test_realistic_zone_polygon(self):
        # The placeholder rectangle shipped in config/cameras.yaml, at the
        # frame scale it is actually used at.
        zone = [(100, 300), (1180, 300), (1180, 700), (100, 700)]
        rng = random.Random(7)
        pts = [(rng.uniform(0, 1280), rng.uniform(0, 720)) for _ in range(2000)]
        self.assertGreater(self._agree(zone, pts), 1900)


if __name__ == "__main__":
    unittest.main()
