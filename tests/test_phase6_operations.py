"""Phase 6 Operations smoke tests — RUNBOOK, cleanup, and backup scripts."""
import importlib.util
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
RUNBOOK_PATH = REPO_ROOT / "docs" / "operations" / "RUNBOOK.md"


def _load_module(name: str, rel_path: str):
    """Load a script module from scripts/ by file path."""
    path = REPO_ROOT / rel_path
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestCleanupBrowserProfiles(unittest.TestCase):
    """tests/test_phase6_operations.py — cleanup_browser_profiles smoke tests"""

    @classmethod
    def setUpClass(cls):
        cls.cleanup = _load_module("cleanup", "scripts/cleanup_browser_profiles.py")

    def _run(self, env_overrides):
        with patch.dict(os.environ, env_overrides, clear=False):
            return self.cleanup.main()

    def test_no_profiles_dir_exits_cleanly(self):
        """Non-existent browser_profiles dir → exit 0, no error."""
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "nonexistent_profiles")
            code = self._run({"BROWSER_PROFILES_DIR": missing})
        self.assertEqual(code, 0)

    def test_removes_stale_profiles(self):
        """Profile dirs older than threshold are removed."""
        with tempfile.TemporaryDirectory() as tmp:
            old_dir = os.path.join(tmp, "profile_old")
            os.makedirs(old_dir)
            # make it appear 2 days old
            old_time = time.time() - 2 * 86400
            os.utime(old_dir, (old_time, old_time))
            code = self._run({"BROWSER_PROFILES_DIR": tmp, "MAX_PROFILE_AGE_DAYS": "1"})
            self.assertEqual(code, 0)
            self.assertFalse(os.path.exists(old_dir))

    def test_keeps_fresh_profiles(self):
        """Profile dirs newer than threshold are preserved."""
        with tempfile.TemporaryDirectory() as tmp:
            fresh_dir = os.path.join(tmp, "profile_fresh")
            os.makedirs(fresh_dir)
            code = self._run({"BROWSER_PROFILES_DIR": tmp, "MAX_PROFILE_AGE_DAYS": "1"})
            self.assertEqual(code, 0)
            self.assertTrue(os.path.exists(fresh_dir))

    def test_custom_age_env_var(self):
        """MAX_PROFILE_AGE_DAYS=0 removes all profile dirs immediately."""
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("p1", "p2"):
                os.makedirs(os.path.join(tmp, name))
            code = self._run({"BROWSER_PROFILES_DIR": tmp, "MAX_PROFILE_AGE_DAYS": "0"})
            self.assertEqual(code, 0)
            remaining = [e for e in os.listdir(tmp) if os.path.isdir(os.path.join(tmp, e))]
            self.assertEqual(remaining, [])

    def test_custom_dir_env_var(self):
        """BROWSER_PROFILES_DIR env var overrides the default directory."""
        with tempfile.TemporaryDirectory() as tmp:
            custom = os.path.join(tmp, "my_profiles")
            # does not exist → should exit cleanly without scanning default
            code = self._run({"BROWSER_PROFILES_DIR": custom})
            self.assertEqual(code, 0)


class TestBackupBillingPool(unittest.TestCase):
    """backup_billing_pool smoke tests"""

    @classmethod
    def setUpClass(cls):
        cls.backup = _load_module("backup", "scripts/backup_billing_pool.py")

    def _run(self, env_overrides):
        with patch.dict(os.environ, env_overrides, clear=False):
            return self.backup.main()

    def test_no_source_dir_exits_cleanly(self):
        """Non-existent source dir → exit 0, no backup created."""
        with tempfile.TemporaryDirectory() as tmp:
            missing_src = os.path.join(tmp, "no_src")
            backup_root = os.path.join(tmp, "backups")
            code = self._run({
                "BILLING_POOL_DIR": missing_src,
                "BILLING_BACKUP_DIR": backup_root,
            })
        self.assertEqual(code, 0)

    def test_creates_timestamped_backup(self):
        """Backup creates a subdir matching YYYYMMDD_HHMMSS pattern."""
        import re
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "pool")
            os.makedirs(src)
            open(os.path.join(src, "a.txt"), "w").close()
            backup_root = os.path.join(tmp, "backups")
            code = self._run({"BILLING_POOL_DIR": src, "BILLING_BACKUP_DIR": backup_root})
            self.assertEqual(code, 0)
            subdirs = os.listdir(backup_root)
            self.assertEqual(len(subdirs), 1)
            self.assertRegex(subdirs[0], r"^\d{8}_\d{6}$")

    def test_copies_txt_files_only(self):
        """Only .txt files are copied; .db and .json are ignored."""
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "pool")
            os.makedirs(src)
            for fname in ("billing.txt", "extra.txt", "db.db", "data.json"):
                open(os.path.join(src, fname), "w").close()
            backup_root = os.path.join(tmp, "backups")
            code = self._run({"BILLING_POOL_DIR": src, "BILLING_BACKUP_DIR": backup_root})
            self.assertEqual(code, 0)
            subdir = os.listdir(backup_root)[0]
            backed_up = set(os.listdir(os.path.join(backup_root, subdir)))
            self.assertEqual(backed_up, {"billing.txt", "extra.txt"})

    def test_respects_max_backups(self):
        """Old backups beyond MAX_BACKUPS are removed."""
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "pool")
            os.makedirs(src)
            open(os.path.join(src, "a.txt"), "w").close()
            backup_root = os.path.join(tmp, "backups")
            os.makedirs(backup_root)
            # pre-create 3 old backup dirs with sortably-older names
            for i in range(1, 4):
                os.makedirs(os.path.join(backup_root, f"20200101_00000{i}"))
            code = self._run({
                "BILLING_POOL_DIR": src,
                "BILLING_BACKUP_DIR": backup_root,
                "MAX_BACKUPS": "3",
            })
            self.assertEqual(code, 0)
            remaining = sorted(os.listdir(backup_root))
            self.assertEqual(len(remaining), 3)

    def test_empty_source_exits_cleanly(self):
        """Source dir exists but has no .txt files → exit 0."""
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "pool")
            os.makedirs(src)
            open(os.path.join(src, "not_txt.db"), "w").close()
            backup_root = os.path.join(tmp, "backups")
            code = self._run({"BILLING_POOL_DIR": src, "BILLING_BACKUP_DIR": backup_root})
            self.assertEqual(code, 0)
            self.assertFalse(os.path.exists(backup_root))

    def test_custom_backup_dir_env_var(self):
        """BILLING_BACKUP_DIR env var overrides the default backup location."""
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "pool")
            os.makedirs(src)
            open(os.path.join(src, "profiles.txt"), "w").close()
            custom_backup = os.path.join(tmp, "custom_backups")
            code = self._run({
                "BILLING_POOL_DIR": src,
                "BILLING_BACKUP_DIR": custom_backup,
            })
            self.assertEqual(code, 0)
            self.assertTrue(os.path.isdir(custom_backup))
            self.assertEqual(len(os.listdir(custom_backup)), 1)


class TestRunbookExists(unittest.TestCase):
    """Verify runbook and scripts exist and are non-empty."""

    def test_runbook_exists(self):
        self.assertTrue(RUNBOOK_PATH.exists(), f"RUNBOOK not found: {RUNBOOK_PATH}")
        self.assertGreater(RUNBOOK_PATH.stat().st_size, 0)

    def test_runbook_has_required_sections(self):
        content = RUNBOOK_PATH.read_text(encoding="utf-8")
        for keyword in ("Start", "Stop", "Cron", "Fallback"):
            self.assertIn(keyword, content, f"Missing section keyword: {keyword!r}")

    def test_cleanup_script_exists(self):
        path = SCRIPTS_DIR / "cleanup_browser_profiles.py"
        self.assertTrue(path.exists(), f"cleanup script not found: {path}")
        self.assertGreater(path.stat().st_size, 0)

    def test_backup_script_exists(self):
        path = SCRIPTS_DIR / "backup_billing_pool.py"
        self.assertTrue(path.exists(), f"backup script not found: {path}")
        self.assertGreater(path.stat().st_size, 0)


if __name__ == "__main__":
    unittest.main()
