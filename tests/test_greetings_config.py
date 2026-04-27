"""Tests for ``_load_greetings`` extensibility (Spec §4).

``_GREETINGS`` must be extensible via the ``GIVEX_GREETINGS_FILE`` env
var without code changes.  The loader merges defaults + file entries
(deduplicated, order preserved) and falls back to defaults — without
raising — when the file is missing or malformed.
"""

import logging
import os
import tempfile
import unittest

from modules.cdp import driver as drv


_BLUEPRINT_REQUIRED = (
    "Happy Birthday!",
    "Best wishes",
    "Enjoy your gift!",
    "Thank you for being you",
)


class TestLoadGreetings(unittest.TestCase):
    def test_file_with_three_entries_merged_with_defaults(self):
        """tmp file with 3 entries → list contains defaults + 3 new."""
        with tempfile.NamedTemporaryFile(
            "w", suffix=".txt", delete=False, encoding="utf-8"
        ) as fh:
            fh.write("Custom greeting 1\nCustom greeting 2\nCustom greeting 3\n")
            path = fh.name
        try:
            merged = drv._load_greetings(path)
        finally:
            os.unlink(path)
        for default in drv._DEFAULT_GREETINGS:
            self.assertIn(default, merged)
        for required in _BLUEPRINT_REQUIRED:
            self.assertIn(required, merged)
        for extra in ("Custom greeting 1", "Custom greeting 2", "Custom greeting 3"):
            self.assertIn(extra, merged)
        self.assertEqual(len(merged), len(drv._DEFAULT_GREETINGS) + 3)
        # Defaults appear before file entries (order preserved).
        self.assertEqual(merged[: len(drv._DEFAULT_GREETINGS)], list(drv._DEFAULT_GREETINGS))

    def test_missing_file_falls_back_to_defaults_no_exception(self):
        """missing file → defaults only, no exception, WARNING logged."""
        missing_path = os.path.join(tempfile.gettempdir(), "definitely_not_here_givex.txt")
        if os.path.exists(missing_path):  # pragma: no cover - hygiene
            os.unlink(missing_path)
        with self.assertLogs(drv._log.name, level=logging.WARNING) as cm:
            merged = drv._load_greetings(missing_path)
        self.assertEqual(merged, list(drv._DEFAULT_GREETINGS))
        # WARNING must name the env var and include the offending path so
        # operators can diagnose misconfiguration from the log.
        self.assertTrue(
            any(
                drv._GREETINGS_FILE_ENV in m and missing_path in m
                for m in cm.output
            ),
            f"expected warning naming {drv._GREETINGS_FILE_ENV} and {missing_path}; got {cm.output!r}",
        )

    def test_empty_file_falls_back_to_defaults(self):
        """empty file → defaults only."""
        with tempfile.NamedTemporaryFile(
            "w", suffix=".txt", delete=False, encoding="utf-8"
        ) as fh:
            path = fh.name  # write nothing
        try:
            merged = drv._load_greetings(path)
        finally:
            os.unlink(path)
        self.assertEqual(merged, list(drv._DEFAULT_GREETINGS))

    def test_duplicate_entries_deduplicated(self):
        """Entries duplicating defaults or each other are dropped, order preserved."""
        with tempfile.NamedTemporaryFile(
            "w", suffix=".txt", delete=False, encoding="utf-8"
        ) as fh:
            fh.write("Happy Birthday!\nNew one\nNew one\n\n")
            path = fh.name
        try:
            merged = drv._load_greetings(path)
        finally:
            os.unlink(path)
        self.assertEqual(merged.count("Happy Birthday!"), 1)
        self.assertEqual(merged.count("New one"), 1)
        self.assertEqual(merged[-1], "New one")

    def test_env_var_used_when_path_omitted(self):
        with tempfile.NamedTemporaryFile(
            "w", suffix=".txt", delete=False, encoding="utf-8"
        ) as fh:
            fh.write("From env\n")
            path = fh.name
        prev = os.environ.get(drv._GREETINGS_FILE_ENV)
        os.environ[drv._GREETINGS_FILE_ENV] = path
        try:
            merged = drv._load_greetings()
        finally:
            if prev is None:
                os.environ.pop(drv._GREETINGS_FILE_ENV, None)
            else:
                os.environ[drv._GREETINGS_FILE_ENV] = prev
            os.unlink(path)
        self.assertIn("From env", merged)

    def test_blueprint_greetings_remain_after_merge(self):
        """All 4 spec example greetings present after merge with extras."""
        with tempfile.NamedTemporaryFile(
            "w", suffix=".txt", delete=False, encoding="utf-8"
        ) as fh:
            fh.write("X\nY\n")
            path = fh.name
        try:
            merged = drv._load_greetings(path)
        finally:
            os.unlink(path)
        for required in _BLUEPRINT_REQUIRED:
            self.assertIn(required, merged)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
