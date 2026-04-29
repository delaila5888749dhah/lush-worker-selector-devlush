"""Startup tests for ``app._startup_load_billing_pool``.

Mirrors the MaxMind GeoIP startup-abort behaviour: in production mode an
empty billing pool must abort startup with ``sys.exit(1)``; in dev/stub mode
it should warn-only and allow startup to continue.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.__main__ import _startup_load_billing_pool
from modules.billing import main as billing

# Production mode rejects /tmp paths for BILLING_POOL_DIR; create temp pool
# directories under the project root instead so the production-mode tests
# exercise the actual pool dir we set up.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]


class StartupBillingPoolTests(unittest.TestCase):
    """Verify the production-mode fail-fast guard for an empty billing pool."""

    def setUp(self):
        billing._reset_state()  # pylint: disable=protected-access

    def tearDown(self):
        billing._reset_state()  # pylint: disable=protected-access

    def test_abort_on_empty_pool_production(self):
        """ENABLE_PRODUCTION_TASK_FN=1 + empty pool dir → SystemExit(1)."""
        with tempfile.TemporaryDirectory(dir=str(_PROJECT_ROOT)) as tmpdir:
            with patch.dict(os.environ, {}, clear=False):
                os.environ["ENABLE_PRODUCTION_TASK_FN"] = "1"
                os.environ["BILLING_POOL_DIR"] = tmpdir
                os.environ.pop("MIN_BILLING_PROFILES", None)
                with self.assertRaises(SystemExit) as cm:
                    _startup_load_billing_pool()
                self.assertEqual(cm.exception.code, 1)

    def test_continue_on_empty_pool_dev(self):
        """ENABLE_PRODUCTION_TASK_FN off + empty pool dir → no exit."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("ENABLE_PRODUCTION_TASK_FN", None)
                os.environ.pop("MIN_BILLING_PROFILES", None)
                os.environ["BILLING_POOL_DIR"] = tmpdir
                # Should not raise.
                _startup_load_billing_pool()

    def test_production_with_populated_pool_does_not_abort(self):
        """ENABLE_PRODUCTION_TASK_FN=1 + non-empty pool → no exit."""
        with tempfile.TemporaryDirectory(dir=str(_PROJECT_ROOT)) as tmpdir:
            pool_file = os.path.join(tmpdir, "pool.txt")
            with open(pool_file, "w", encoding="utf-8") as f:
                f.write("Alice|Smith|1 St|City|NY|10001|2125550001|a@e.com\n")
                f.write("Bob|Jones|2 St|City|CA|90210|3105550002|b@e.com\n")
            with patch.dict(os.environ, {}, clear=False):
                os.environ["ENABLE_PRODUCTION_TASK_FN"] = "1"
                os.environ["BILLING_POOL_DIR"] = tmpdir
                os.environ.pop("MIN_BILLING_PROFILES", None)
                # Should not raise.
                _startup_load_billing_pool()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

