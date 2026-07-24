import unittest

from finblade.geometry import foot_point, point_in_polygon


SQUARE = [(0, 0), (10, 0), (10, 10), (0, 10)]


class TestFootPoint(unittest.TestCase):
    def test_bottom_center(self):
        self.assertEqual(foot_point(10, 20, 30, 80), (20.0, 80.0))

    def test_uses_lower_y_as_ground(self):
        # y2 < y1 (swapped) still returns the larger y as the ground contact.
        self.assertEqual(foot_point(0, 100, 40, 60), (20.0, 100.0))


class TestPointInPolygon(unittest.TestCase):
    def test_inside(self):
        self.assertTrue(point_in_polygon((5, 5), SQUARE))

    def test_outside(self):
        self.assertFalse(point_in_polygon((15, 5), SQUARE))
        self.assertFalse(point_in_polygon((-1, -1), SQUARE))

    def test_on_edge_is_inside(self):
        self.assertTrue(point_in_polygon((5, 0), SQUARE))    # bottom edge
        self.assertTrue(point_in_polygon((10, 5), SQUARE))   # right edge

    def test_on_vertex_is_inside(self):
        self.assertTrue(point_in_polygon((0, 0), SQUARE))

    def test_concave_polygon(self):
        # An L-shape; a point in the notch must be outside.
        L = [(0, 0), (10, 0), (10, 4), (4, 4), (4, 10), (0, 10)]
        self.assertTrue(point_in_polygon((2, 2), L))
        self.assertFalse(point_in_polygon((8, 8), L))  # in the missing corner

    def test_degenerate_polygon(self):
        self.assertFalse(point_in_polygon((0, 0), [(0, 0), (1, 1)]))


if __name__ == "__main__":
    unittest.main()
