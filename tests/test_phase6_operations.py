"""Phase 6 Operations smoke tests — RUNBOOK, cleanup, and backup scripts."""
import importlib.util
import os
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
    path = (REPO_ROOT / rel_path).resolve()
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None:
        raise ImportError(f"Could not create import spec for module {name!r} at {path}")
    if spec.loader is None:
        raise ImportError(f"Could not load module {name!r}: no loader available for {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestCleanupBrowserProfiles(unittest.TestCase):
    """tests/test_phase6_operations.py — cleanup_browser_profiles smoke tests"""

    @classmethod
    def setUpClass(cls):
        cls.cleanup = None
        cls.cleanup_path = SCRIPTS_DIR / "cleanup_browser_profiles.py"
        cls._load_error = None

    def _get_module(self):
        if self.cleanup is not None:
            return self.cleanup
        if not self.cleanup_path.exists():
            self.fail(f"Missing cleanup script: {self.cleanup_path}")
        if self._load_error is not None:
            self.fail(f"Failed to import cleanup script: {self._load_error}")
        try:
            type(self).cleanup = _load_module("cleanup", "scripts/cleanup_browser_profiles.py")
        except Exception as exc:
            type(self)._load_error = exc
            self.fail(f"Failed to import cleanup script: {exc}")
        return self.cleanup

    def _run(self, env_overrides):
        mod = self._get_module()
        with patch.dict(os.environ, env_overrides, clear=False):
            return mod.main()

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
        """MAX_PROFILE_AGE_DAYS=1 removes dirs whose mtime is set to 2 days ago."""
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("p1", "p2"):
                d = os.path.join(tmp, name)
                os.makedirs(d)
                old_time = time.time() - 2 * 86400
                os.utime(d, (old_time, old_time))
            code = self._run({"BROWSER_PROFILES_DIR": tmp, "MAX_PROFILE_AGE_DAYS": "1"})
            self.assertEqual(code, 0)
            remaining = [e for e in os.listdir(tmp) if os.path.isdir(os.path.join(tmp, e))]
            self.assertEqual(remaining, [])

    def test_custom_dir_env_var(self):
        """BROWSER_PROFILES_DIR env var overrides the default directory."""
        with tempfile.TemporaryDirectory() as tmp:
            custom = os.path.join(tmp, "my_profiles")
            code = self._run({"BROWSER_PROFILES_DIR": custom})
            self.assertEqual(code, 0)


class TestBackupBillingPool(unittest.TestCase):
    """backup_billing_pool smoke tests"""

    @classmethod
    def setUpClass(cls):
        cls.backup = None
        cls.backup_path = SCRIPTS_DIR / "backup_billing_pool.py"
        cls._load_error = None

    def _get_module(self):
        if self.backup is not None:
            return self.backup
        if not self.backup_path.exists():
            self.fail(f"Missing backup script: {self.backup_path}")
        if self._load_error is not None:
            self.fail(f"Failed to import backup script: {self._load_error}")
        try:
            type(self).backup = _load_module("backup", "scripts/backup_billing_pool.py")
        except Exception as exc:
            type(self)._load_error = exc
            self.fail(f"Failed to import backup script: {exc}")
        return self.backup

    def _run(self, env_overrides):
        mod = self._get_module()
        with patch.dict(os.environ, env_overrides, clear=False):
            return mod.main()

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
        """Backup creates a subdir matching YYYYMMDD_HHMMSS_ffffff pattern."""
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "pool")
            os.makedirs(src)
            Path(src, "a.txt").touch()
            backup_root = os.path.join(tmp, "backups")
            code = self._run({"BILLING_POOL_DIR": src, "BILLING_BACKUP_DIR": backup_root})
            self.assertEqual(code, 0)
            subdirs = os.listdir(backup_root)
            self.assertEqual(len(subdirs), 1)
            self.assertRegex(subdirs[0], r"^\d{8}_\d{6}_\d+$")

    def test_copies_txt_files_only(self):
        """Only .txt files are copied; .db and .json are ignored."""
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "pool")
            os.makedirs(src)
            for fname in ("billing.txt", "extra.txt", "db.db", "data.json"):
                Path(src, fname).touch()
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
            Path(src, "a.txt").touch()
            backup_root = os.path.join(tmp, "backups")
            os.makedirs(backup_root)
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
            Path(src, "not_txt.db").touch()
            backup_root = os.path.join(tmp, "backups")
            code = self._run({"BILLING_POOL_DIR": src, "BILLING_BACKUP_DIR": backup_root})
            self.assertEqual(code, 0)
            self.assertFalse(os.path.exists(backup_root))

    def test_custom_backup_dir_env_var(self):
        """BILLING_BACKUP_DIR env var overrides the default backup location."""
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "pool")
            os.makedirs(src)
            Path(src, "profiles.txt").touch()
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

    def test_backup_via_cli_subprocess_smoke(self):
        """Run backup_billing_pool.py as a subprocess against a temp pool dir."""
        import subprocess  # nosec B404  # pylint: disable=import-outside-toplevel
        import sys  # pylint: disable=import-outside-toplevel
        with tempfile.TemporaryDirectory() as tmp_dir:
            pool_dir = Path(tmp_dir) / "pool"
            pool_dir.mkdir()
            (pool_dir / "billing_pool_0001.txt").write_text(
                "Jane|Doe|addr|City|ST|00000||\n", encoding="utf-8",
            )
            backup_dir = Path(tmp_dir) / "backups"
            env = os.environ.copy()
            env["BILLING_POOL_DIR"] = str(pool_dir)
            env["BILLING_BACKUP_DIR"] = str(backup_dir)
            proc = subprocess.run(  # nosec B603
                [sys.executable, str(SCRIPTS_DIR / "backup_billing_pool.py")],
                capture_output=True, text=True, check=False, env=env,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(backup_dir.exists())
            snapshots = [p for p in backup_dir.iterdir() if p.is_dir()]
            self.assertEqual(len(snapshots), 1)
            self.assertTrue((snapshots[0] / "billing_pool_0001.txt").exists())

    def test_cleanup_via_cli_subprocess_smoke(self):
        """Run cleanup_browser_profiles.py as a subprocess against a temp dir."""
        import subprocess  # nosec B404  # pylint: disable=import-outside-toplevel
        import sys  # pylint: disable=import-outside-toplevel
        with tempfile.TemporaryDirectory() as tmp_dir:
            profiles_dir = Path(tmp_dir) / "profiles"
            profiles_dir.mkdir()
            stale = profiles_dir / "old_profile"
            stale.mkdir()
            (stale / "placeholder.txt").write_text("x", encoding="utf-8")
            # Set mtime to 2 days in the past so the default cutoff removes it.
            old_ts = time.time() - 2 * 86400
            os.utime(stale, (old_ts, old_ts))
            env = os.environ.copy()
            env["BROWSER_PROFILES_DIR"] = str(profiles_dir)
            env["MAX_PROFILE_AGE_DAYS"] = "1"
            proc = subprocess.run(  # nosec B603
                [sys.executable, str(SCRIPTS_DIR / "cleanup_browser_profiles.py")],
                capture_output=True, text=True, check=False, env=env,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertFalse(stale.exists())


if __name__ == "__main__":
    unittest.main()
