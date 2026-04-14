"""Tests for modules/cdp/mouse.py — GhostCursor and build_path.

Covers:
- build_path: determinism under a fixed seed, length, final target point,
  jitter bounds.
- GhostCursor.move_to: CDP mouseMoved dispatch per waypoint, position
  tracking after move, graceful skip of failed CDP calls, deterministic
  output under a fixed seed.
- GhostCursor.position: initial state, updated after move_to.
"""

import random
import unittest
from unittest.mock import MagicMock, patch

from modules.cdp.mouse import GhostCursor, build_path


def _rnd(seed: int = 0) -> random.Random:
    """Return a seeded Random instance."""
    r = random.Random()
    r.seed(seed)
    return r


class TestBuildPath(unittest.TestCase):
    """build_path returns a deterministic Bézier-like waypoint list."""

    def test_returns_n_plus_one_points(self):
        path = build_path((0.0, 0.0), (200.0, 100.0), _rnd(1), n_points=5)
        self.assertEqual(len(path), 6)

    def test_last_point_is_exact_target(self):
        path = build_path((10.0, 20.0), (300.0, 150.0), _rnd(7), n_points=4)
        self.assertEqual(path[-1], (300.0, 150.0))

    def test_deterministic_under_fixed_seed(self):
        path_a = build_path((0.0, 0.0), (200.0, 100.0), _rnd(42), n_points=5)
        path_b = build_path((0.0, 0.0), (200.0, 100.0), _rnd(42), n_points=5)
        self.assertEqual(path_a, path_b)

    def test_different_seeds_produce_different_paths(self):
        path_a = build_path((0.0, 0.0), (200.0, 100.0), _rnd(1), n_points=5)
        path_b = build_path((0.0, 0.0), (200.0, 100.0), _rnd(2), n_points=5)
        self.assertNotEqual(path_a[:-1], path_b[:-1])

    def test_intermediate_points_have_jitter(self):
        """Intermediate waypoints include jitter so they are not on a strict line."""
        n = 10
        rnd = _rnd(99)
        path = build_path((0.0, 0.0), (100.0, 50.0), rnd, n_points=n)
        intermediate = path[:-1]
        # At least one intermediate point must deviate from the straight line
        on_line = all(
            abs(x - 100.0 * (i + 1) / (n + 1)) < 1e-9
            and abs(y - 50.0 * (i + 1) / (n + 1)) < 1e-9
            for i, (x, y) in enumerate(intermediate)
        )
        self.assertFalse(on_line, "Expected jitter on intermediate points")

    def test_non_zero_start_shifts_path(self):
        path_from_zero = build_path((0.0, 0.0), (100.0, 50.0), _rnd(5), n_points=3)
        path_from_offset = build_path((50.0, 25.0), (100.0, 50.0), _rnd(5), n_points=3)
        # The exact target is the same but intermediate points differ
        self.assertNotEqual(path_from_zero[:-1], path_from_offset[:-1])


class TestGhostCursorPosition(unittest.TestCase):
    """GhostCursor tracks the logical cursor position across moves."""

    def test_initial_position_is_origin(self):
        driver = MagicMock()
        gc = GhostCursor(driver, _rnd(0))
        self.assertEqual(gc.position, (0.0, 0.0))

    def test_position_updated_after_move_to(self):
        driver = MagicMock()
        gc = GhostCursor(driver, _rnd(0))
        with patch("time.sleep"):
            gc.move_to(150.0, 75.0)
        self.assertEqual(gc.position, (150.0, 75.0))

    def test_successive_moves_chain_positions(self):
        driver = MagicMock()
        gc = GhostCursor(driver, _rnd(0))
        with patch("time.sleep"):
            gc.move_to(100.0, 50.0)
            gc.move_to(200.0, 80.0)
        self.assertEqual(gc.position, (200.0, 80.0))


class TestGhostCursorDispatch(unittest.TestCase):
    """GhostCursor dispatches CDP mouseMoved events for each waypoint."""

    def test_dispatches_mousemoved_per_waypoint(self):
        driver = MagicMock()
        gc = GhostCursor(driver, _rnd(42))
        with patch("time.sleep"):
            gc.move_to(200.0, 100.0, n_points=4)
        # 4 intermediate + 1 target = 5 total mouseMoved calls
        self.assertEqual(driver.execute_cdp_cmd.call_count, 5)
        for c in driver.execute_cdp_cmd.call_args_list:
            self.assertEqual(c[0][0], "Input.dispatchMouseEvent")
            self.assertEqual(c[0][1]["type"], "mouseMoved")
            self.assertEqual(c[0][1]["button"], "none")
            self.assertEqual(c[0][1]["clickCount"], 0)

    def test_last_waypoint_is_exact_target(self):
        driver = MagicMock()
        gc = GhostCursor(driver, _rnd(7))
        with patch("time.sleep"):
            gc.move_to(300.0, 150.0, n_points=3)
        last_call = driver.execute_cdp_cmd.call_args_list[-1]
        params = last_call[0][1]
        self.assertEqual(params["x"], 300.0)
        self.assertEqual(params["y"], 150.0)

    def test_deterministic_waypoints_under_fixed_seed(self):
        def get_dispatched_coordinates(rnd_seed: int):
            driver = MagicMock()
            coords = []
            driver.execute_cdp_cmd.side_effect = lambda _cmd, p: coords.append((p["x"], p["y"]))
            gc = GhostCursor(driver, _rnd(rnd_seed))
            with patch("time.sleep"):
                gc.move_to(200.0, 100.0, n_points=5)
            return coords

        self.assertEqual(get_dispatched_coordinates(42), get_dispatched_coordinates(42))
        self.assertNotEqual(get_dispatched_coordinates(1), get_dispatched_coordinates(2))

    def test_failed_cdp_call_does_not_raise(self):
        driver = MagicMock()
        driver.execute_cdp_cmd.side_effect = RuntimeError("CDP unavailable")
        gc = GhostCursor(driver, _rnd(0))
        with patch("time.sleep"):
            gc.move_to(100.0, 50.0, n_points=3)  # Should not raise
        # Position is still updated even when all CDP calls fail
        self.assertEqual(gc.position, (100.0, 50.0))

    def test_partial_cdp_failure_continues_remaining_waypoints(self):
        driver = MagicMock()
        call_count = [0]

        def flaky_cdp(_cmd, _params):
            call_count[0] += 1
            if call_count[0] == 2:
                raise RuntimeError("transient failure")

        driver.execute_cdp_cmd.side_effect = flaky_cdp
        gc = GhostCursor(driver, _rnd(3))
        with patch("time.sleep"):
            gc.move_to(100.0, 50.0, n_points=4)  # 5 total calls
        self.assertEqual(driver.execute_cdp_cmd.call_count, 5)
        self.assertEqual(gc.position, (100.0, 50.0))

    def test_sleep_called_per_waypoint(self):
        driver = MagicMock()
        sleep_calls = []

        def record_sleep(delay):
            sleep_calls.append(delay)

        gc = GhostCursor(driver, _rnd(0))
        with patch("time.sleep", side_effect=record_sleep):
            gc.move_to(100.0, 50.0, n_points=4, click_delay=0.07)
        # One sleep per waypoint (4 intermediate + 1 target = 5)
        self.assertEqual(len(sleep_calls), 5)
        for val in sleep_calls:
            self.assertAlmostEqual(val, 0.07)
