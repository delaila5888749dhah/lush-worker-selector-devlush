"""P1-4 — clear_card_fields_cdp raises CDPError on CDP failure.

Previously the method swallowed CDP exceptions with a log warning, which
could leave stale card data in the form and lead to a double-charge on
submit. This test pins the new contract: if the underlying CDP command
fails, the method MUST raise :class:`~modules.common.exceptions.CDPError`
so the orchestrator retry loop can abort the cycle instead of resubmitting.
"""

import unittest
from unittest.mock import MagicMock, patch

from modules.cdp.driver import GivexDriver
from modules.common.exceptions import CDPError


def _make_driver():
    driver = MagicMock()
    element = MagicMock()
    driver.find_elements.return_value = [element]
    return driver


class TestClearCardFieldsCdpP14(unittest.TestCase):
    """Regression tests for P1-4 CDP clear failure propagation."""

    def test_execute_cdp_cmd_raise_bubbles_as_cdp_error(self):
        """execute_cdp_cmd failures must surface as CDPError with chaining."""
        driver = _make_driver()
        driver.execute_cdp_cmd.side_effect = RuntimeError("cdp target crashed")
        givex_driver = GivexDriver(driver)
        with patch.object(givex_driver, "bounding_box_click"):
            with self.assertRaises(CDPError) as ctx:
                givex_driver.clear_card_fields_cdp()
        # The original CDP exception must be chained (raise ... from exc).
        self.assertIsInstance(ctx.exception.__cause__, RuntimeError)

    def test_bounding_box_click_raise_bubbles_as_cdp_error(self):
        """bounding_box_click failures must also surface as chained CDPError."""
        driver = _make_driver()
        givex_driver = GivexDriver(driver)
        with patch.object(
            givex_driver, "bounding_box_click", side_effect=RuntimeError("click failed")
        ):
            with self.assertRaises(CDPError) as ctx:
                givex_driver.clear_card_fields_cdp()
        self.assertIsInstance(ctx.exception.__cause__, RuntimeError)


if __name__ == "__main__":
    unittest.main()
