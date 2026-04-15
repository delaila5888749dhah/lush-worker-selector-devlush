"""Tests for scripts/seed_billing_pool.py and scripts/download_maxmind.py."""
import hashlib
import importlib.util
import io
import os
import subprocess  # nosec B404
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _load_module(name: str, rel_path: str):
    """Load a script module from the repo by relative path."""
    path = (REPO_ROOT / rel_path).resolve()
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError("Could not load module %r from %s" % (name, path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeResponse(io.BytesIO):
    """Minimal context-manager wrapper around BytesIO for mocking urlopen."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


class ScriptTests(unittest.TestCase):
    """Smoke tests for billing pool seed and MaxMind download scripts."""

    def test_seed_billing_pool_cli_creates_txt_output(self):
        """CLI creates .txt with correct pipe-delimited output and skips short rows."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            input_path = os.path.join(tmp_dir, "profiles.csv")
            output_dir = os.path.join(tmp_dir, "pool")
            with open(input_path, "w", encoding="utf-8") as handle:
                handle.write(
                "Jane,Doe,123 Main St,Austin,TX,78701,5551231234,jane@example.com\n"
                "Only,Five,Fields,Will,Skip\n"
                "John,Smith,45 Oak Rd,Dallas,TX,75001\n",
                )
            proc = subprocess.run(  # nosec B603
                [sys.executable, str(SCRIPTS_DIR / "seed_billing_pool.py"),
                 "--input", input_path, "--output", output_dir],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            txt_files = [
                os.path.join(output_dir, name)
                for name in os.listdir(output_dir)
                if name.endswith(".txt")
            ]
            self.assertGreaterEqual(len(txt_files), 1)
            with open(txt_files[0], "r", encoding="utf-8") as handle:
                lines = handle.read().strip().splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(
                lines[0],
                "Jane|Doe|123 Main St|Austin|TX|78701|5551231234|jane@example.com",
            )
            self.assertEqual(lines[1], "John|Smith|45 Oak Rd|Dallas|TX|75001||")

    def test_download_maxmind_cli_without_license_key_exits_1(self):
        """Missing MAXMIND_LICENSE_KEY env var → exit code 1 with error."""
        env = os.environ.copy()
        env.pop("MAXMIND_LICENSE_KEY", None)
        proc = subprocess.run(  # nosec B603
            [sys.executable, str(SCRIPTS_DIR / "download_maxmind.py")],
            capture_output=True, text=True, check=False, env=env,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("MAXMIND_LICENSE_KEY", proc.stderr)

    def test_download_maxmind_main_downloads_and_saves_mmdb(self):
        """Mock download verifies checksum, extracts, and saves .mmdb."""
        module = _load_module("download_maxmind", "scripts/download_maxmind.py")
        mmdb_payload = b"mmdb-test-bytes"
        archive_stream = io.BytesIO()
        with tarfile.open(fileobj=archive_stream, mode="w:gz") as tar:
            info = tarfile.TarInfo(name="GeoLite2-City_20260414/GeoLite2-City.mmdb")
            info.size = len(mmdb_payload)
            tar.addfile(info, io.BytesIO(mmdb_payload))
        archive_bytes = archive_stream.getvalue()
        checksum = hashlib.sha256(archive_bytes).hexdigest()
        checksum_bytes = ("%s  GeoLite2-City.tar.gz\n" % checksum).encode()

        def _mock_urlopen(url, **_kw):
            text = getattr(url, "full_url", url)
            if str(text).endswith("suffix=tar.gz.sha256"):
                return _FakeResponse(checksum_bytes)
            if str(text).endswith("suffix=tar.gz"):
                return _FakeResponse(archive_bytes)
            raise AssertionError("Unexpected URL: %s" % text)

        with tempfile.TemporaryDirectory() as tmp_dir:
            saved_cwd = os.getcwd()
            try:
                os.chdir(tmp_dir)
                with patch.dict(os.environ, {"MAXMIND_LICENSE_KEY": "test_key"}):
                    with patch.object(module.urllib.request, "urlopen",
                                      side_effect=_mock_urlopen):
                        code = module.main()
                self.assertEqual(code, 0)
                mmdb_path = os.path.join(tmp_dir, "data", "GeoLite2-City.mmdb")
                self.assertTrue(os.path.exists(mmdb_path))
                with open(mmdb_path, "rb") as handle:
                    self.assertEqual(handle.read(), mmdb_payload)
            finally:
                os.chdir(saved_cwd)


if __name__ == "__main__":
    unittest.main()
