"""T-11 — 4×4 burst typing wired when filling the card number.

``GivexDriver._realistic_type_field`` must call ``generate_4x4_pattern``
(NOT ``generate_burst_pattern``) when the card-number field is being filled
with ``use_burst=True`` and a 16-digit PAN.
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.cdp.driver import GivexDriver, SEL_CARD_NUMBER  # noqa: E402
from _e2e_harness import E2EBase  # noqa: E402


class TestT11FourByFourBurstWired(E2EBase):
    """T-11: 4×4 typing wired when filling card (use_burst=True)."""

    def test_card_number_fill_uses_4x4_pattern(self):
        raw = MagicMock()
        raw.find_elements.return_value = [MagicMock()]
        gd = GivexDriver(raw)
        bio = MagicMock()
        bio.generate_4x4_pattern.return_value = [0.05] * 16
        bio.generate_burst_pattern.return_value = [0.07]
        setattr(gd, "_bio", bio)

        with patch("modules.cdp.driver._type_value") as mock_type, \
             patch.object(gd, "_human_scroll_to"), \
             patch.object(gd, "_wait_scroll_stable"), \
             patch.object(gd, "bounding_box_click"), \
             patch.object(gd, "_verify_field_value_length"), \
             patch.object(gd, "_engine_aware_sleep"):
            gd._realistic_type_field(
                SEL_CARD_NUMBER,
                "4111111111111111",
                use_burst=True,
                field_kind="card_number",
            )

        bio.generate_4x4_pattern.assert_called_once()
        bio.generate_burst_pattern.assert_not_called()
        # Delays forwarded into the typing helper must be the 4×4 pattern.
        self.assertEqual(mock_type.call_args.kwargs["delays"], [0.05] * 16)


if __name__ == "__main__":
    unittest.main()
