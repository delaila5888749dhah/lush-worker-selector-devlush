import math
import random
import unittest

from modules.cdp.mouse import build_path


class TestBezierCurvature(unittest.TestCase):
    def test_build_path_is_curved(self):
        start = (0.0, 0.0)
        target = (400.0, 200.0)
        rnd = random.Random(42)
        path = build_path(start, target, rnd, n_points=20)

        sx, sy = start
        tx, ty = target
        dx = tx - sx
        dy = ty - sy
        line_len = math.hypot(dx, dy)
        # Perpendicular distance from each intermediate waypoint to the
        # straight start->end line.
        max_perp = 0.0
        for x, y in path[:-1]:
            # |(dx)(sy - y) - (sx - x)(dy)| / line_len
            perp = abs(dx * (sy - y) - (sx - x) * dy) / line_len
            if perp > max_perp:
                max_perp = perp
        self.assertGreaterEqual(max_perp, 5.0)

    def test_build_path_endpoint_exact(self):
        start = (0.0, 0.0)
        target = (400.0, 200.0)
        rnd = random.Random(42)
        path = build_path(start, target, rnd, n_points=20)
        self.assertEqual(path[-1], target)

    def test_build_path_deterministic_per_seed(self):
        start = (0.0, 0.0)
        target = (400.0, 200.0)
        path1 = build_path(start, target, random.Random(123), n_points=20)
        path2 = build_path(start, target, random.Random(123), n_points=20)
        self.assertEqual(path1, path2)

    def test_build_path_n_points_count(self):
        start = (0.0, 0.0)
        target = (400.0, 200.0)
        rnd = random.Random(42)
        n_points = 20
        path = build_path(start, target, rnd, n_points=n_points)
        self.assertEqual(len(path), n_points + 1)


if __name__ == "__main__":
    unittest.main()
