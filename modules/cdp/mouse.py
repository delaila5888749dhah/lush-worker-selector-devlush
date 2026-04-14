"""Mouse interaction helpers for CDP-based cursor movement.

Provides ``GhostCursor`` — a dedicated helper that generates Bézier-like
movement paths and dispatches them as CDP ``Input.dispatchMouseEvent``
(type ``mouseMoved``) events.

Isolates path generation and movement dispatch from the driver integration
layer so that both are independently testable and reusable.
"""

import logging
import math
import time

_log = logging.getLogger(__name__)


def build_path(start, target, rnd, n_points: int):
    """Generate a cubic Bézier waypoint list from *start* to *target*.

    Computes two randomised control points offset perpendicularly from the
    straight line so the resulting path has real curvature rather than
    jittered linear interpolation.  The final point is exactly *target*.

    Args:
        start: ``(x, y)`` starting coordinate in viewport pixels.
        target: ``(x, y)`` destination coordinate in viewport pixels.
        rnd: A ``random.Random``-compatible instance for reproducible paths.
        n_points: Number of intermediate waypoints before the exact target.

    Returns:
        List of ``(x, y)`` tuples with ``n_points + 1`` entries; the last
        entry is exactly *target*.
    """
    sx, sy = start
    tx, ty = target
    dx = tx - sx
    dy = ty - sy
    # Perpendicular offset vector (rotate 90°, scaled by curve strength).
    if hasattr(rnd, "getstate") and hasattr(rnd, "setstate"):
        state = rnd.getstate()
        _sign = 1 if rnd.random() > 0.5 else -1
        rnd.setstate(state)
    else:
        _sign = 1 if rnd.random() > 0.5 else -1
    perp_scale = _sign * rnd.uniform(0.15, 0.45)
    px = -dy * perp_scale
    py = dx * perp_scale
    # Cubic Bézier control points.
    cp1x = sx + dx * rnd.uniform(0.20, 0.40) + px
    cp1y = sy + dy * rnd.uniform(0.20, 0.40) + py
    cp2x = sx + dx * rnd.uniform(0.60, 0.80) + px * 0.5
    cp2y = sy + dy * rnd.uniform(0.60, 0.80) + py * 0.5
    points = []
    for i in range(1, n_points + 1):
        t = i / (n_points + 1)
        u = 1.0 - t
        x = u**3 * sx + 3.0*u**2*t * cp1x + 3.0*u*t**2 * cp2x + t**3 * tx
        y = u**3 * sy + 3.0*u**2*t * cp1y + 3.0*u*t**2 * cp2y + t**3 * ty
        points.append((x, y))
    points.append((tx, ty))
    return points


class GhostCursor:
    """Dispatches cursor movement via CDP ``mouseMoved`` events along a path.

    Maintains the current logical cursor position so that successive
    ``move_to()`` calls form a continuous path across the viewport.
    Path generation is deterministic under a fixed persona seed.

    Args:
        driver: Selenium WebDriver instance (or compatible mock) that
            exposes ``execute_cdp_cmd``.
        rnd: A ``random.Random``-compatible instance used for path
            generation.  Deterministic output requires a seeded instance.
    """

    def __init__(
            self, driver: object, rnd,
            viewport_width: int = 1280,
            viewport_height: int = 720) -> None:
        self._driver = driver
        self._rnd = rnd
        self._x: float = self._rnd.uniform(viewport_width * 0.1, viewport_width * 0.9)
        self._y: float = self._rnd.uniform(viewport_height * 0.1, viewport_height * 0.9)

    @property
    def position(self):
        """Current logical cursor position as ``(x, y)``."""
        return self._x, self._y

    def move_to(
        self,
        target_x: float,
        target_y: float,
        *,
        n_points=None,  # type: int | None
        click_delay=None,  # type: float | None
    ) -> None:
        """Move cursor to ``(target_x, target_y)`` via CDP mouseMoved events.

        Dispatches one ``Input.dispatchMouseEvent`` per waypoint along the
        generated path, then updates the stored cursor position.  Failed
        individual waypoints are logged and skipped; the cursor position is
        still updated after the path completes.

        Args:
            target_x: Destination X coordinate in viewport pixels.
            target_y: Destination Y coordinate in viewport pixels.
            n_points: Intermediate waypoints to generate.  Defaults to a
                random integer in ``[4, 8]``.
            click_delay: Per-waypoint sleep in seconds. If ``None``, delay is
                dynamically scaled by movement distance.
        """
        if n_points is None:
            n_points = self._rnd.randint(4, 8)
        if click_delay is None:
            base_dist = math.hypot(target_x - self._x, target_y - self._y)
            if hasattr(self._rnd, "getstate") and hasattr(self._rnd, "setstate"):
                state = self._rnd.getstate()
                jitter = self._rnd.uniform(0.8, 1.2)
                self._rnd.setstate(state)
            else:
                jitter = self._rnd.uniform(0.8, 1.2)
            click_delay = max(0.01, min(0.12, base_dist / 5000.0)) * jitter

        path = build_path((self._x, self._y), (target_x, target_y), self._rnd, n_points)

        for px, py in path:
            try:
                self._driver.execute_cdp_cmd(
                    "Input.dispatchMouseEvent",
                    {
                        "type": "mouseMoved",
                        "x": px,
                        "y": py,
                        "button": "none",
                        "clickCount": 0,
                    },
                )
            except Exception:
                _log.debug("GhostCursor.move_to: CDP mouseMoved skipped", exc_info=True)
            time.sleep(click_delay)

        self._x, self._y = target_x, target_y

    def scroll_wheel(self, delta_y: float, *, steps: int = 4) -> None:
        """Dispatch CDP mouseWheel events in *steps* incremental steps."""
        if steps < 1:
            steps = 1
        step_delta = delta_y / steps
        for _ in range(steps):
            try:
                self._driver.execute_cdp_cmd(
                    "Input.dispatchMouseEvent",
                    {"type": "mouseWheel", "x": self._x, "y": self._y,
                     "deltaX": 0.0, "deltaY": step_delta},
                )
            except Exception:
                _log.debug("GhostCursor.scroll_wheel: CDP skipped", exc_info=True)
            time.sleep(self._rnd.uniform(0.02, 0.06))
