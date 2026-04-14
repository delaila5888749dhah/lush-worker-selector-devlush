"""Mouse interaction helpers for CDP-based cursor movement.

Provides ``GhostCursor`` — a dedicated helper that generates Bézier-like
movement paths and dispatches them as CDP ``Input.dispatchMouseEvent``
(type ``mouseMoved``) events.

Isolates path generation and movement dispatch from the driver integration
layer so that both are independently testable and reusable.
"""

import logging
import time

_log = logging.getLogger(__name__)


def build_path(start, target, rnd, n_points: int):
    """Generate a Bézier-like waypoint list from *start* to *target*.

    Uses linear interpolation with per-point random jitter to simulate
    natural cursor travel.  The final point is exactly *target*.

    Args:
        start: ``(x, y)`` starting coordinate in viewport pixels.
        target: ``(x, y)`` destination coordinate in viewport pixels.
        rnd: A ``random.Random``-compatible instance for reproducible paths.
        n_points: Number of intermediate jitter waypoints to insert before
            the exact target point.

    Returns:
        List of ``(x, y)`` tuples with ``n_points + 1`` entries; the last
        entry is exactly *target*.
    """
    start_x, start_y = start
    target_x, target_y = target
    points = []
    for i in range(1, n_points + 1):
        t = i / (n_points + 1)
        x = start_x + (target_x - start_x) * t + rnd.uniform(-30, 30)
        y = start_y + (target_y - start_y) * t + rnd.uniform(-20, 20)
        points.append((x, y))
    points.append((target_x, target_y))
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

    def __init__(self, driver: object, rnd) -> None:
        self._driver = driver
        self._rnd = rnd
        self._x: float = 0.0
        self._y: float = 0.0

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
        click_delay: float = 0.05,
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
            click_delay: Per-waypoint sleep in seconds.
        """
        if n_points is None:
            n_points = self._rnd.randint(4, 8)

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
