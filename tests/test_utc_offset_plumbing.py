"""Tests for UTC offset plumbing (Phase 5B Task 1, Blueprint §10).

Covers:
  - TemporalModel.get_time_state respects MaxMind-derived UTC offset
  - apply_temporal_modifier produces different multipliers when offset
    flips DAY → NIGHT for the same UTC time
  - Missing/invalid offset gracefully defaults to 0
  - ContextVar set by `integration.worker_task` propagates to TemporalModel
"""
import contextvars
import math
import time
import unittest
from unittest.mock import patch

from modules.delay.persona import PersonaProfile
from modules.delay.temporal import (
    TemporalModel,
    set_utc_offset,
    get_utc_offset,
    _utc_offset_var,
)


def _frozen_gmt(hour: int):
    return time.struct_time((2026, 1, 1, hour, 0, 0, 3, 1, 0))


class TestTemporalUsesMaxmindOffset(unittest.TestCase):
    """22:00 UTC should be NIGHT at offset=0 but DAY at offset=-8 (PST)."""

    def setUp(self):
        # Force a persona with a wide DAY window covering 14:00 PST.
        self.persona = PersonaProfile(42)
        self.persona.active_hours = (6, 21)  # DAY = 06..21 inclusive
        self.tm = TemporalModel(self.persona)

    def test_utc_only_is_night_at_22h(self):
        with patch("modules.delay.temporal.time.gmtime", return_value=_frozen_gmt(22)):
            self.assertEqual(self.tm.get_time_state(0), "NIGHT")

    def test_pst_offset_minus_8_makes_22h_utc_a_day_state(self):
        """22:00 UTC + (-8h) = 14:00 local PST → DAY."""
        with patch("modules.delay.temporal.time.gmtime", return_value=_frozen_gmt(22)):
            self.assertEqual(self.tm.get_time_state(-8), "DAY")

    def test_half_hour_offset_preserves_boundary_precision(self):
        """00:30 UTC + 5.5h = 06:00 local, which must stay inside the DAY window."""
        frozen = time.struct_time((2026, 1, 1, 0, 30, 0, 3, 1, 0))
        with patch("modules.delay.temporal.time.gmtime", return_value=frozen):
            self.assertEqual(self.tm.get_time_state(5.5), "DAY")

    def test_negative_half_hour_offset_preserves_boundary_precision(self):
        """09:00 UTC + (-3.5h) = 05:30 local, which must remain NIGHT."""
        frozen = time.struct_time((2026, 1, 1, 9, 0, 0, 3, 1, 0))
        with patch("modules.delay.temporal.time.gmtime", return_value=frozen):
            self.assertEqual(self.tm.get_time_state(-3.5), "NIGHT")


class TestDelayVariesWithUtcOffset(unittest.TestCase):
    """Same persona, same UTC time, different offsets → different multipliers."""

    def setUp(self):
        self.persona = PersonaProfile(7)
        self.persona.active_hours = (6, 21)

    def test_delay_varies_with_utc_offset(self):
        tm = TemporalModel(self.persona)
        base = 1.0
        with patch(
            "modules.delay.temporal.time.gmtime", return_value=_frozen_gmt(22)
        ):
            night_delay = tm.apply_temporal_modifier(base, "typing", utc_offset_hours=0)
        # Fresh model so RNG state matches; exercise DAY path next.
        tm2 = TemporalModel(self.persona)
        with patch(
            "modules.delay.temporal.time.gmtime", return_value=_frozen_gmt(22)
        ):
            day_delay = tm2.apply_temporal_modifier(base, "typing", utc_offset_hours=-8)
        # NIGHT typing delay > DAY (which is exactly base since no drift, no penalty).
        self.assertGreater(night_delay, day_delay)
        self.assertEqual(day_delay, base)


class TestUtcOffsetContextVarPropagates(unittest.TestCase):
    """ContextVar set by worker_task propagates to TemporalModel.apply_temporal_modifier."""

    def setUp(self):
        # Reset the ContextVar for each test (run inside a fresh context).
        self._token = _utc_offset_var.set(0.0)

    def tearDown(self):
        _utc_offset_var.reset(self._token)

    def test_set_and_get_utc_offset(self):
        set_utc_offset(-5.0)
        self.assertAlmostEqual(get_utc_offset(), -5.0)

    def test_apply_temporal_modifier_reads_context_var_when_arg_missing(self):
        """apply_temporal_modifier(...) without explicit offset reads ContextVar."""
        persona = PersonaProfile(42)
        persona.active_hours = (6, 21)
        tm = TemporalModel(persona)
        # Force NIGHT in UTC at 22:00; DAY in PST (-8h).
        with patch(
            "modules.delay.temporal.time.gmtime", return_value=_frozen_gmt(22)
        ):
            # Default offset (0) → NIGHT path.
            set_utc_offset(0.0)
            night = tm.apply_temporal_modifier(1.0, "typing")
            # PST offset → DAY path.
            tm_day = TemporalModel(persona)
            set_utc_offset(-8.0)
            day = tm_day.apply_temporal_modifier(1.0, "typing")
        self.assertGreater(night, day)

    def test_context_var_isolates_between_contexts(self):
        """contextvars.copy_context provides isolation between threads/tasks."""
        set_utc_offset(-5.0)

        def _inner():
            set_utc_offset(7.0)
            return get_utc_offset()

        # Run _inner in an isolated context — it should NOT leak back.
        ctx = contextvars.copy_context()
        result = ctx.run(_inner)
        self.assertEqual(result, 7.0)
        self.assertEqual(get_utc_offset(), -5.0)


class TestMissingMaxmindOffsetDefaultsTo0(unittest.TestCase):
    """Graceful fallback when MaxMind cannot resolve an offset."""

    def setUp(self):
        self._token = _utc_offset_var.set(0.0)

    def tearDown(self):
        _utc_offset_var.reset(self._token)

    def test_default_offset_is_zero(self):
        self.assertEqual(get_utc_offset(), 0.0)

    def test_invalid_offset_value_falls_back_to_zero(self):
        """set_utc_offset() with non-numeric input must not crash; falls back to 0.0."""
        set_utc_offset("not-a-number")  # type: ignore[arg-type]
        self.assertEqual(get_utc_offset(), 0.0)

    def test_none_offset_value_falls_back_to_zero(self):
        """set_utc_offset() accepts None defensively and resets to UTC."""
        set_utc_offset(None)  # type: ignore[arg-type]
        self.assertEqual(get_utc_offset(), 0.0)

    def test_non_finite_offset_values_fall_back_to_zero(self):
        """NaN/inf offsets are rejected so later delay math cannot crash."""
        for value in (math.nan, math.inf, -math.inf):
            set_utc_offset(value)
            self.assertEqual(get_utc_offset(), 0.0)


if __name__ == "__main__":
    unittest.main()
