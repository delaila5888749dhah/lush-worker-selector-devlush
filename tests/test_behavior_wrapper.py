"""Tests for BehaviorWrapper — Task 10.5."""
import time
import unittest
from unittest.mock import patch

from modules.delay.main import PersonaProfile, wrap


def _dummy_task(worker_id):
    """Simple task that returns a known value."""
    return f"ok-{worker_id}"


def _failing_task(worker_id):
    raise RuntimeError("boom")


class TestWrapPreservesReturnValue(unittest.TestCase):
    def test_return_value_unchanged(self):
        persona = PersonaProfile(42)
        wrapped = wrap(_dummy_task, persona)
        result = wrapped("w-1")
        self.assertEqual(result, "ok-w-1")


class TestWrapAddsDelay(unittest.TestCase):
    def test_measurable_delay(self):
        persona = PersonaProfile(42)
        wrapped = wrap(_dummy_task, persona)
        with patch("modules.delay.wrapper.time.sleep") as mock_sleep:
            wrapped("w-1")
            mock_sleep.assert_called_once()
            delay_arg = mock_sleep.call_args[0][0]
            self.assertGreater(delay_arg, 0.0, "sleep should be called with positive delay")


class TestWrapPropagatesExceptions(unittest.TestCase):
    def test_exception_propagated(self):
        persona = PersonaProfile(42)
        wrapped = wrap(_failing_task, persona)
        with self.assertRaises(RuntimeError):
            wrapped("w-1")


class TestDeterminism(unittest.TestCase):
    def test_same_seed_same_behavior(self):
        p1 = PersonaProfile(99)
        p2 = PersonaProfile(99)
        w1 = wrap(_dummy_task, p1)
        w2 = wrap(_dummy_task, p2)
        self.assertEqual(w1("w-1"), w2("w-1"))


if __name__ == "__main__":
    unittest.main()
