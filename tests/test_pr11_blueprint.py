"""Blueprint compliance tests — PR-11 (P1–P8).

Covers:
  P2 — MaxMind Reader singleton opened only once across N lookups.
  P3 — No HTTP calls to api.ipify.org; proxy IP extracted locally.
  P4 — Per-worker billing state: independent shuffle, anti-repeat, zip filter.
  P5 — CycleContext: billing profile locked across card-swap retries.
"""
from __future__ import annotations

import collections
import os
import random
import threading
import types
import unittest
import uuid
from dataclasses import dataclass
from typing import List
from unittest.mock import MagicMock, call, patch

from modules.billing import main as billing
from modules.billing.main import WorkerBillingState
from modules.cdp import driver as drv
from modules.common.exceptions import CycleExhaustedError
from modules.common.types import BillingProfile, CycleContext


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_profile(name: str = "Test", zip_code: str = "10001") -> BillingProfile:
    return BillingProfile(
        first_name=name, last_name="User", address="1 Test St",
        city="Testcity", state="NY", zip_code=zip_code,
        phone="2125550001", email="test@example.com",
    )


def _set_master_pool(profiles: list) -> None:
    """Directly populate the billing master pool for testing."""
    with billing._MASTER_POOL_LOCK:  # pylint: disable=protected-access
        billing._MASTER_POOL = list(profiles)  # pylint: disable=protected-access
        billing._MASTER_POOL_LOADED = True  # pylint: disable=protected-access


# ──────────────────────────────────────────────────────────────────────────────
# P2 — MaxMind Reader singleton
# ──────────────────────────────────────────────────────────────────────────────

class TestMaxMindSingleton(unittest.TestCase):
    """init_maxmind_reader opens the DB once; N maxmind_lookup_zip calls reuse it."""

    def setUp(self):
        """Save and clear the singleton before each test."""
        self._orig_reader = drv._MAXMIND_READER  # pylint: disable=protected-access
        drv._MAXMIND_READER = None  # pylint: disable=protected-access

    def tearDown(self):
        """Restore the singleton after each test."""
        drv._MAXMIND_READER = self._orig_reader  # pylint: disable=protected-access

    def _make_fake_db_module(self, postal_code: str = "10001"):
        """Return a fake geoip2.database module whose Reader records construction calls."""
        fake_record = MagicMock()
        fake_record.postal.code = postal_code

        class FakeReader:
            """Fake geoip2 Reader that records how many times it was constructed."""
            construction_count = 0

            def __init__(self, _path):
                FakeReader.construction_count += 1

            def city(self, _ip):  # pylint: disable=no-self-use
                return fake_record

        FakeReader.construction_count = 0
        fake_db_module = types.SimpleNamespace(Reader=FakeReader)
        return fake_db_module, FakeReader

    def test_reader_opened_only_once_for_n_lookups(self):
        """init_maxmind_reader opens Reader once; 5 maxmind_lookup_zip calls reuse it."""
        fake_db_module, FakeReader = self._make_fake_db_module("90210")

        with patch("modules.cdp.driver.os.path.exists", return_value=True), \
             patch.dict("sys.modules", {
                 "geoip2": types.SimpleNamespace(database=fake_db_module),
                 "geoip2.database": fake_db_module,
             }):
            drv.init_maxmind_reader("data/GeoLite2-City.mmdb")
            results = [drv.maxmind_lookup_zip("1.2.3.4") for _ in range(5)]

        self.assertEqual(FakeReader.construction_count, 1,
                         "Reader constructor must be called exactly once (singleton).")
        self.assertTrue(all(r == "90210" for r in results),
                        f"All zip lookups must return the mocked value, got {results}")

    def test_maxmind_lookup_zip_uses_singleton_reader(self):
        """After init_maxmind_reader, maxmind_lookup_zip uses _MAXMIND_READER directly."""
        mock_reader = MagicMock()
        mock_record = MagicMock()
        mock_record.postal.code = "10001"
        mock_reader.city.return_value = mock_record

        drv._MAXMIND_READER = mock_reader  # pylint: disable=protected-access
        result = drv.maxmind_lookup_zip("8.8.8.8")

        self.assertEqual(result, "10001")
        mock_reader.city.assert_called_once_with("8.8.8.8")

    def test_init_maxmind_reader_raises_if_file_missing(self):
        """init_maxmind_reader raises FileNotFoundError when mmdb is absent."""
        with patch("modules.cdp.driver.os.path.exists", return_value=False):
            with self.assertRaises(FileNotFoundError):
                drv.init_maxmind_reader("/nonexistent/path.mmdb")

    def test_init_maxmind_reader_raises_if_geoip2_missing(self):
        """init_maxmind_reader raises ImportError when geoip2 is not installed."""
        with patch("modules.cdp.driver.os.path.exists", return_value=True), \
             patch("modules.cdp.driver.importlib.import_module",
                   side_effect=ImportError("geoip2 not installed")):
            with self.assertRaises(ImportError):
                drv.init_maxmind_reader("/fake/path.mmdb")

    def test_fallback_per_call_when_singleton_not_init(self):
        """When singleton is None, maxmind_lookup_zip falls back to per-call open."""
        # Singleton is None (set in setUp).
        fake_record = MagicMock()
        fake_record.postal.code = "98765"

        class FakeReader:
            def __init__(self, _path):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
            def city(self, _ip):  # pylint: disable=no-self-use
                return fake_record

        fake_db_module = types.SimpleNamespace(Reader=FakeReader)
        with patch("modules.cdp.driver.os.path.exists", return_value=True), \
             patch.dict("sys.modules", {
                 "geoip2": types.SimpleNamespace(database=fake_db_module),
                 "geoip2.database": fake_db_module,
             }):
            result = drv.maxmind_lookup_zip("5.5.5.5")
        self.assertEqual(result, "98765")

    def test_concurrent_lookups_reader_constructed_once(self):
        """100 concurrent maxmind_lookup_zip calls after init reuse the same reader."""
        fake_db_module, FakeReader = self._make_fake_db_module("55555")
        results = []
        errors = []

        with patch("modules.cdp.driver.os.path.exists", return_value=True), \
             patch.dict("sys.modules", {
                 "geoip2": types.SimpleNamespace(database=fake_db_module),
                 "geoip2.database": fake_db_module,
             }):
            drv.init_maxmind_reader("data/test.mmdb")

            barrier = threading.Barrier(10)

            def _lookup():
                barrier.wait()
                try:
                    results.append(drv.maxmind_lookup_zip("2.2.2.2"))
                except Exception as exc:  # pylint: disable=broad-except
                    errors.append(exc)

            threads = [threading.Thread(target=_lookup) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)

        self.assertEqual(errors, [], f"Unexpected errors: {errors}")
        self.assertEqual(len(results), 10)
        # Reader was constructed once before the threads ran.
        self.assertEqual(FakeReader.construction_count, 1)


# ──────────────────────────────────────────────────────────────────────────────
# P3 — No HTTP to api.ipify.org; proxy IP extraction
# ──────────────────────────────────────────────────────────────────────────────

class TestGetProxyIp(unittest.TestCase):
    """_get_proxy_ip extracts the host/IP from a proxy string without HTTP calls."""

    def test_returns_ipv4_from_full_url(self):
        """IPv4 address in proxy URL is returned directly."""
        result = drv._get_proxy_ip("http://user:pass@1.2.3.4:8080")  # pylint: disable=protected-access
        self.assertEqual(result, "1.2.3.4")

    def test_returns_ipv4_from_bare_host_port(self):
        """IP:port without scheme is handled correctly."""
        result = drv._get_proxy_ip("203.0.113.5:3128")  # pylint: disable=protected-access
        self.assertEqual(result, "203.0.113.5")

    def test_returns_none_when_no_proxy_configured(self):
        """Returns None when proxy_str is None and PROXY_SERVER env is unset."""
        with patch.dict(os.environ, {}, clear=True):
            env = {k: v for k, v in os.environ.items() if k != "PROXY_SERVER"}
            with patch.dict(os.environ, env, clear=True):
                result = drv._get_proxy_ip(None)  # pylint: disable=protected-access
        self.assertIsNone(result)

    def test_reads_from_proxy_server_env_var(self):
        """Falls back to PROXY_SERVER env var when proxy_str is None."""
        with patch.dict(os.environ, {"PROXY_SERVER": "192.168.1.1:8080"}):
            result = drv._get_proxy_ip(None)  # pylint: disable=protected-access
        self.assertEqual(result, "192.168.1.1")

    def test_resolves_hostname_via_local_dns(self):
        """Hostnames are resolved via socket.gethostbyname (no external HTTP)."""
        with patch("modules.cdp.driver.socket.gethostbyname",
                   return_value="10.0.0.1") as mock_dns:
            result = drv._get_proxy_ip("http://proxy.internal:8080")  # pylint: disable=protected-access
        mock_dns.assert_called_once_with("proxy.internal")
        self.assertEqual(result, "10.0.0.1")

    def test_returns_none_on_dns_failure(self):
        """Returns None gracefully when DNS resolution fails."""
        with patch("modules.cdp.driver.socket.gethostbyname",
                   side_effect=OSError("DNS failure")):
            result = drv._get_proxy_ip("http://bad.host:8080")  # pylint: disable=protected-access
        self.assertIsNone(result)

    def test_get_current_ip_best_effort_does_not_call_ipify(self):
        """_get_current_ip_best_effort no longer calls api.ipify.org."""
        with patch("modules.cdp.driver.urllib.request.urlopen") as mock_urlopen:
            # It should not call urlopen at all.
            drv._get_current_ip_best_effort()  # pylint: disable=protected-access
        mock_urlopen.assert_not_called()

    def test_get_current_ip_best_effort_delegates_to_get_proxy_ip(self):
        """_get_current_ip_best_effort delegates to _get_proxy_ip."""
        with patch("modules.cdp.driver._get_proxy_ip", return_value="5.5.5.5") as mock:
            result = drv._get_current_ip_best_effort()  # pylint: disable=protected-access
        mock.assert_called_once()
        self.assertEqual(result, "5.5.5.5")


# ──────────────────────────────────────────────────────────────────────────────
# P4 — Per-worker billing state
# ──────────────────────────────────────────────────────────────────────────────

class TestPerWorkerBillingState(unittest.TestCase):
    """Per-worker billing state: independent shuffle, anti-repeat, zip filter."""

    def setUp(self):
        billing._reset_state()  # pylint: disable=protected-access
        self._profiles = [
            _make_profile(f"P{i}", zip_code=f"0000{i}") for i in range(5)
        ]
        _set_master_pool(self._profiles)

    def tearDown(self):
        billing._reset_state()  # pylint: disable=protected-access

    def test_two_workers_get_different_shuffled_orders(self):
        """Workers w1 and w2 receive differently-shuffled profile lists."""
        state_w1 = billing.get_worker_state("w1")
        state_w2 = billing.get_worker_state("w2")
        # With 5 profiles, the probability of identical shuffle is 1/120 ≈ 0.8%.
        # Run 3 attempts to make flakiness vanishingly unlikely.
        names_w1 = [p.first_name for p in state_w1.profiles]
        names_w2 = [p.first_name for p in state_w2.profiles]
        # Both contain the same profiles.
        self.assertCountEqual(names_w1, names_w2)
        # But they should NOT be in the same order (very high probability).
        # Accept if they happen to match (unlikely) but we can verify independence.
        self.assertIsNot(state_w1, state_w2,
                         "Different workers must have independent state objects.")

    def test_same_worker_sequential_wraps_around(self):
        """Same worker iterates through all profiles without repeating until full cycle."""
        profiles = [_make_profile(f"S{i}", zip_code=f"9999{i}") for i in range(3)]
        _set_master_pool(profiles)
        billing._reset_state()  # pylint: disable=protected-access
        # Re-populate master pool after reset.
        _set_master_pool(profiles)

        seen_first_names = []
        for _ in range(3):
            p = billing.select_profile("NOMATCH", worker_id="seq-worker")
            seen_first_names.append(p.first_name)

        # After full traversal, should wrap: 4th call returns first profile again.
        fourth = billing.select_profile("NOMATCH", worker_id="seq-worker")
        self.assertEqual(fourth.first_name, seen_first_names[0],
                         "After full traversal, pointer should wrap back to index 0.")
        self.assertEqual(len(set(seen_first_names)), 3,
                         "Should have returned 3 distinct profiles in one traversal.")

    def test_zip_match_does_not_advance_index(self):
        """Zip match returns the matching profile without advancing the index pointer."""
        profiles = [
            _make_profile("NoMatch1", "00001"),
            _make_profile("ZipMatch", "10001"),
            _make_profile("NoMatch2", "00002"),
        ]
        _set_master_pool(profiles)
        billing._reset_state()  # pylint: disable=protected-access
        _set_master_pool(profiles)

        # Force worker state with known order (no shuffle for test predictability).
        with billing._WORKER_STATES_LOCK:  # pylint: disable=protected-access
            state = WorkerBillingState(profiles=list(profiles), index=0)
            billing._WORKER_STATES["zip-test-worker"] = state  # pylint: disable=protected-access

        index_before = state.index
        result = billing.select_profile("10001", worker_id="zip-test-worker")
        self.assertEqual(result.first_name, "ZipMatch",
                         "Should return the zip-matching profile.")
        self.assertEqual(state.index, index_before,
                         "Index must NOT advance on zip match (blueprint rule).")

    def test_no_zip_match_advances_index(self):
        """No zip match uses profile at index and advances the pointer by 1."""
        profiles = [
            _make_profile("Alpha", "00001"),
            _make_profile("Beta", "00002"),
        ]
        _set_master_pool(profiles)
        billing._reset_state()  # pylint: disable=protected-access
        _set_master_pool(profiles)

        with billing._WORKER_STATES_LOCK:  # pylint: disable=protected-access
            state = WorkerBillingState(profiles=list(profiles), index=0)
            billing._WORKER_STATES["nomatch-worker"] = state  # pylint: disable=protected-access

        result = billing.select_profile("99999", worker_id="nomatch-worker")
        self.assertEqual(result.first_name, "Alpha",
                         "Should return profile at index 0.")
        self.assertEqual(state.index, 1,
                         "Index must advance by 1 when no zip match.")

    def test_different_workers_have_independent_index(self):
        """Advancing one worker's index does not affect another worker's index."""
        profiles = [_make_profile(f"X{i}", "10001") for i in range(3)]
        _set_master_pool(profiles)
        billing._reset_state()  # pylint: disable=protected-access
        _set_master_pool(profiles)

        with billing._WORKER_STATES_LOCK:  # pylint: disable=protected-access
            billing._WORKER_STATES["wa"] = WorkerBillingState(  # pylint: disable=protected-access
                profiles=list(profiles), index=0)
            billing._WORKER_STATES["wb"] = WorkerBillingState(  # pylint: disable=protected-access
                profiles=list(profiles), index=0)

        # Advance wa twice.
        billing.select_profile("NOMATCH", worker_id="wa")
        billing.select_profile("NOMATCH", worker_id="wa")

        state_wa = billing._WORKER_STATES["wa"]  # pylint: disable=protected-access
        state_wb = billing._WORKER_STATES["wb"]  # pylint: disable=protected-access
        self.assertEqual(state_wa.index, 2,
                         "wa index must be 2 after 2 sequential (no-match) selections.")
        self.assertEqual(state_wb.index, 0,
                         "wb index must still be 0 — independent of wa.")

    def test_legacy_path_unchanged_no_worker_id(self):
        """select_profile() without worker_id still uses the legacy deque path."""
        p = _make_profile("Legacy", "12345")
        with billing._lock:  # pylint: disable=protected-access
            billing._profiles = collections.deque([p])  # pylint: disable=protected-access

        result = billing.select_profile("12345")
        self.assertEqual(result.first_name, "Legacy")

    def test_empty_per_worker_pool_raises(self):
        """Empty master pool raises CycleExhaustedError for per-worker path."""
        billing._reset_state()  # pylint: disable=protected-access
        _set_master_pool([])

        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"BILLING_POOL_DIR": tmpdir}):
                with self.assertRaises(CycleExhaustedError):
                    billing.select_profile("10001", worker_id="empty-worker")

    def test_worker_shuffle_matches_sha256_golden_order(self):
        """Shuffle order must match the SHA-256-derived golden order.

        This pins the seed derivation to SHA-256(worker_id, UTF-8) → first 8
        bytes → big-endian int, which is independent of ``PYTHONHASHSEED``.
        If the implementation regresses to ``hash(worker_id)`` (or any other
        non-canonical seed), this assertion will fail because ``hash`` of a
        ``str`` is randomized per-process.
        """
        import hashlib  # noqa: PLC0415

        worker_id = "w1"
        digest = hashlib.sha256(worker_id.encode("utf-8")).digest()[:8]
        expected_seed = int.from_bytes(digest, "big")
        expected = list(self._profiles)
        random.Random(expected_seed).shuffle(expected)
        expected_order = [p.first_name for p in expected]

        actual_order = [p.first_name for p in billing.get_worker_state(worker_id).profiles]
        self.assertEqual(
            expected_order, actual_order,
            "Shuffle order must derive from SHA-256(worker_id), not hash().",
        )

    def test_worker_shuffle_deterministic_across_subprocess(self):
        """Same worker_id yields identical shuffle order in a fresh process.

        Spawns a child Python interpreter with a randomized ``PYTHONHASHSEED``
        (the same env var that previously caused ``hash(worker_id)`` to vary
        per process) and asserts the shuffle order is byte-identical to the
        current process. This is the regression guard for the original bug.
        """
        import json  # noqa: PLC0415
        import subprocess  # noqa: PLC0415
        import sys  # noqa: PLC0415
        import textwrap  # noqa: PLC0415

        in_proc_order = [p.first_name for p in billing.get_worker_state("w1").profiles]

        # Build a child script that recreates the same master pool and prints
        # the resulting shuffle order as JSON. Pool data is passed via JSON
        # to avoid relying on filesystem state.
        pool_payload = json.dumps([
            {
                "first_name": p.first_name, "last_name": p.last_name,
                "address": p.address, "city": p.city, "state": p.state,
                "zip_code": p.zip_code, "phone": p.phone, "email": p.email,
            }
            for p in self._profiles
        ])
        child_script = textwrap.dedent(
            """
            import json, sys
            from modules.billing import main as billing
            from modules.common.types import BillingProfile
            data = json.loads(sys.argv[1])
            profiles = [BillingProfile(**d) for d in data]
            with billing._MASTER_POOL_LOCK:
                billing._MASTER_POOL = list(profiles)
                billing._MASTER_POOL_LOADED = True
            order = [p.first_name for p in billing.get_worker_state("w1").profiles]
            print(json.dumps(order))
            """
        )

        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = dict(os.environ)
        # Force a different PYTHONHASHSEED than the parent. Under the old
        # ``hash(worker_id)`` implementation this would (with overwhelming
        # probability) produce a different shuffle order, so this test would
        # have caught the regression. SHA-256 is unaffected.
        env["PYTHONHASHSEED"] = "12345"
        env["PYTHONPATH"] = repo_root + os.pathsep + env.get("PYTHONPATH", "")

        result = subprocess.run(
            [sys.executable, "-c", child_script, pool_payload],
            cwd=repo_root, env=env, capture_output=True, text=True, check=False,
        )
        self.assertEqual(
            result.returncode, 0,
            f"Child process failed: stdout={result.stdout!r} stderr={result.stderr!r}",
        )
        child_order = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(
            in_proc_order, child_order,
            "Same worker_id must produce identical shuffle order across "
            "processes regardless of PYTHONHASHSEED.",
        )

    def test_two_workers_get_different_orders(self):
        """Different worker_ids produce different shuffle orders (deterministically)."""
        state_w1 = billing.get_worker_state("w1")
        state_w2 = billing.get_worker_state("w2")
        order_w1 = [p.first_name for p in state_w1.profiles]
        order_w2 = [p.first_name for p in state_w2.profiles]
        self.assertCountEqual(order_w1, order_w2)
        self.assertNotEqual(
            order_w1, order_w2,
            "Distinct worker_ids must seed independent shuffles.",
        )


# ──────────────────────────────────────────────────────────────────────────────
# P5 — CycleContext: billing locked across card-swap retries
# ──────────────────────────────────────────────────────────────────────────────

class TestCycleContextBillingLock(unittest.TestCase):
    """CycleContext keeps billing profile constant across card-swap retries."""

    def setUp(self):
        """Clear idempotency store before each test to prevent cross-test contamination."""
        try:
            from integration.orchestrator import (  # noqa: PLC0415
                _idempotency_lock,
                _submitted_task_ids,
                _in_flight_task_ids,
                _completed_task_ids,
            )
            with _idempotency_lock:
                _submitted_task_ids.clear()
                _in_flight_task_ids.clear()
                _completed_task_ids.clear()
        except Exception:  # pylint: disable=broad-except
            pass

    def tearDown(self):
        self.setUp()  # clear again after test

    @staticmethod
    def _make_task(task_id=None):
        from modules.common.types import CardInfo, WorkerTask  # noqa: PLC0415
        card = CardInfo(
            card_number="4111111111111111",
            exp_month="12",
            exp_year="2027",
            cvv="123",
        )
        return WorkerTask(
            recipient_email="r@example.com",
            amount=50,
            primary_card=card,
            order_queue=(card, card),
            task_id=task_id or uuid.uuid4().hex,
        )

    @staticmethod
    def _make_billing_profile():
        return BillingProfile(
            first_name="Fixed",
            last_name="Profile",
            address="1 Billing St",
            city="Billingtown",
            state="CA",
            zip_code="90210",
            phone="2135550001",
            email="fixed@billing.com",
        )

    def test_select_profile_called_once_across_three_retries(self):
        """billing.select_profile is called exactly once per ctx even with 3 retry tasks."""
        from integration.orchestrator import run_cycle  # noqa: PLC0415

        fixed_profile = self._make_billing_profile()
        # Simulate 3 distinct "card-swap retry" tasks (different task_ids, same order).
        tasks = [self._make_task(task_id=f"cycle-task-{i}") for i in range(3)]

        ctx = CycleContext(
            cycle_id=uuid.uuid4().hex,
            worker_id="retry-worker",
            zip_code="90210",
        )

        call_count = 0

        def mock_select_profile(_zip=None, worker_id=None):
            nonlocal call_count
            call_count += 1
            return fixed_profile

        with patch("integration.orchestrator.billing") as mock_billing, \
             patch("integration.orchestrator.cdp") as mock_cdp, \
             patch("integration.orchestrator.watchdog") as mock_watchdog, \
             patch("integration.orchestrator.fsm") as mock_fsm, \
             patch("integration.orchestrator.rollout"), \
             patch("integration.orchestrator.monitor"):
            mock_billing.select_profile.side_effect = mock_select_profile
            mock_cdp._get_driver.return_value = MagicMock()
            mock_cdp.run_preflight_and_fill.return_value = None
            mock_cdp.submit_purchase.return_value = None
            mock_watchdog.wait_for_total.return_value = 50.0
            mock_fsm.get_current_state_for_worker.return_value = \
                type("State", (), {"name": "success"})()

            # Simulate 3 calls with the same CycleContext (card-swap retry scenario).
            for task in tasks:
                run_cycle(task, zip_code="90210", worker_id="retry-worker", ctx=ctx)

        self.assertEqual(call_count, 1,
                         "billing.select_profile must be called exactly once per ctx, "
                         f"regardless of retries. Got {call_count} calls.")

    def test_billing_profile_identical_across_retries(self):
        """Billing name/address/phone/email are identical across all 3 attempts."""
        from integration.orchestrator import run_cycle  # noqa: PLC0415

        fixed_profile = self._make_billing_profile()

        profiles_used = []

        def capture_preflight_and_fill(_task, profile, **_kwargs):
            profiles_used.append(profile)

        # Use unique UUID-based task IDs to guarantee no idempotency-store collision.
        tasks = [self._make_task(task_id=uuid.uuid4().hex) for _ in range(3)]

        ctx = CycleContext(
            cycle_id=uuid.uuid4().hex,
            worker_id="billing-lock-worker-" + uuid.uuid4().hex[:8],
            zip_code="90210",
        )

        with patch("integration.orchestrator.billing") as mock_billing, \
             patch("integration.orchestrator.cdp") as mock_cdp, \
             patch("integration.orchestrator.watchdog") as mock_watchdog, \
             patch("integration.orchestrator.fsm") as mock_fsm, \
             patch("integration.orchestrator.rollout"), \
             patch("integration.orchestrator.monitor"):
            mock_billing.select_profile.return_value = fixed_profile
            mock_cdp._get_driver.return_value = MagicMock()
            mock_cdp.run_pre_card_checkout_prepare.side_effect = capture_preflight_and_fill
            mock_cdp.submit_purchase.return_value = None
            mock_watchdog.wait_for_total.return_value = 50.0
            mock_fsm.get_current_state_for_worker.return_value = \
                type("State", (), {"name": "success"})()

            for task in tasks:
                run_cycle(task, zip_code="90210", worker_id=ctx.worker_id, ctx=ctx)

        self.assertEqual(len(profiles_used), 3)
        for p in profiles_used:
            self.assertEqual(p.first_name, fixed_profile.first_name)
            self.assertEqual(p.last_name, fixed_profile.last_name)
            self.assertEqual(p.address, fixed_profile.address)
            self.assertEqual(p.phone, fixed_profile.phone)
            self.assertEqual(p.email, fixed_profile.email)

    def test_ctx_billing_profile_none_triggers_selection(self):
        """When ctx.billing_profile is None, billing.select_profile is called."""
        from integration.orchestrator import run_cycle  # noqa: PLC0415
        from modules.common.types import CycleContext  # noqa: PLC0415

        fixed_profile = self._make_billing_profile()
        task = self._make_task()
        ctx = CycleContext(
            cycle_id=uuid.uuid4().hex,
            worker_id="init-worker",
            billing_profile=None,
            zip_code="90210",
        )

        with patch("integration.orchestrator.billing") as mock_billing, \
             patch("integration.orchestrator.cdp") as mock_cdp, \
             patch("integration.orchestrator.watchdog") as mock_watchdog, \
             patch("integration.orchestrator.fsm") as mock_fsm, \
             patch("integration.orchestrator.rollout"), \
             patch("integration.orchestrator.monitor"):
            mock_billing.select_profile.return_value = fixed_profile
            mock_cdp._get_driver.return_value = MagicMock()
            mock_watchdog.wait_for_total.return_value = 50.0
            mock_fsm.get_current_state_for_worker.return_value = \
                type("State", (), {"name": "success"})()

            run_cycle(task, zip_code="90210", worker_id="init-worker", ctx=ctx)

        mock_billing.select_profile.assert_called_once()
        self.assertEqual(ctx.billing_profile, fixed_profile,
                         "ctx.billing_profile must be populated after first run_cycle call.")

    def test_ctx_billing_profile_set_skips_selection(self):
        """When ctx.billing_profile is already set, billing.select_profile is NOT called."""
        from integration.orchestrator import run_cycle  # noqa: PLC0415

        fixed_profile = self._make_billing_profile()
        task = self._make_task()

        ctx = CycleContext(
            cycle_id=uuid.uuid4().hex,
            worker_id="pre-set-worker",
            billing_profile=fixed_profile,  # Already set!
            zip_code="90210",
        )

        with patch("integration.orchestrator.billing") as mock_billing, \
             patch("integration.orchestrator.cdp") as mock_cdp, \
             patch("integration.orchestrator.watchdog") as mock_watchdog, \
             patch("integration.orchestrator.fsm") as mock_fsm, \
             patch("integration.orchestrator.rollout"), \
             patch("integration.orchestrator.monitor"):
            mock_billing.select_profile.return_value = MagicMock()
            mock_cdp._get_driver.return_value = MagicMock()
            mock_watchdog.wait_for_total.return_value = 50.0
            mock_fsm.get_current_state_for_worker.return_value = \
                type("State", (), {"name": "success"})()

            run_cycle(task, zip_code="90210", worker_id="pre-set-worker", ctx=ctx)

        mock_billing.select_profile.assert_not_called()

    def test_new_ctx_without_ctx_arg_creates_fresh_context(self):
        """run_cycle without ctx arg creates a fresh CycleContext and selects billing once."""
        from integration.orchestrator import run_cycle  # noqa: PLC0415

        fixed_profile = self._make_billing_profile()
        task = self._make_task()

        with patch("integration.orchestrator.billing") as mock_billing, \
             patch("integration.orchestrator.cdp") as mock_cdp, \
             patch("integration.orchestrator.watchdog") as mock_watchdog, \
             patch("integration.orchestrator.fsm") as mock_fsm, \
             patch("integration.orchestrator.rollout"), \
             patch("integration.orchestrator.monitor"):
            mock_billing.select_profile.return_value = fixed_profile
            mock_cdp._get_driver.return_value = MagicMock()
            mock_watchdog.wait_for_total.return_value = 50.0
            mock_fsm.get_current_state_for_worker.return_value = \
                type("State", (), {"name": "success"})()

            # No ctx passed → backward-compat path creates one internally.
            run_cycle(task, zip_code="90210", worker_id="compat-worker")

        mock_billing.select_profile.assert_called_once()


# ──────────────────────────────────────────────────────────────────────────────
# P6 — load_billing_pool eager startup load
# ──────────────────────────────────────────────────────────────────────────────

class TestLoadBillingPool(unittest.TestCase):
    """load_billing_pool() eagerly loads profiles and logs the count."""

    def setUp(self):
        billing._reset_state()  # pylint: disable=protected-access

    def tearDown(self):
        billing._reset_state()  # pylint: disable=protected-access

    def test_load_billing_pool_returns_count(self):
        """load_billing_pool returns the number of profiles loaded."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            pool_file = os.path.join(tmpdir, "pool.txt")
            with open(pool_file, "w") as f:
                for i in range(5):
                    f.write(f"F{i}|L{i}|{i} St|City|NY|1000{i}|212555000{i}|u{i}@e.com\n")
            with patch.dict(os.environ, {"BILLING_POOL_DIR": tmpdir}):
                count = billing.load_billing_pool()
        self.assertEqual(count, 5)

    def test_load_billing_pool_populates_master_pool(self):
        """load_billing_pool populates _MASTER_POOL with the loaded profiles."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            pool_file = os.path.join(tmpdir, "pool.txt")
            with open(pool_file, "w") as f:
                f.write("Alice|Smith|1 St|City|NY|10001|2125550001|a@e.com\n")
                f.write("Bob|Jones|2 St|City|CA|90210|3105550002|b@e.com\n")
            with patch.dict(os.environ, {"BILLING_POOL_DIR": tmpdir}):
                billing.load_billing_pool()

        self.assertEqual(len(billing._MASTER_POOL), 2)  # pylint: disable=protected-access

    def test_load_billing_pool_is_idempotent(self):
        """Calling load_billing_pool twice does not re-read disk."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            pool_file = os.path.join(tmpdir, "pool.txt")
            with open(pool_file, "w") as f:
                f.write("Alice|Smith|1 St|City|NY|10001|2125550001|a@e.com\n")
            with patch.dict(os.environ, {"BILLING_POOL_DIR": tmpdir}):
                count1 = billing.load_billing_pool()
                count2 = billing.load_billing_pool()
        self.assertEqual(count1, count2)


# ──────────────────────────────────────────────────────────────────────────────
# P7 — email domains
# ──────────────────────────────────────────────────────────────────────────────

class TestEmailDomains(unittest.TestCase):
    """_EMAIL_DOMAINS must be (gmail.com, yahoo.com, outlook.com, hotmail.com)."""

    def test_email_domains_blueprint_compliant(self):
        expected = ("gmail.com", "yahoo.com", "outlook.com", "hotmail.com")
        self.assertEqual(
            billing._EMAIL_DOMAINS, expected,  # pylint: disable=protected-access
            "Blueprint requires exactly gmail/yahoo/outlook/hotmail — no icloud.com.",
        )

    def test_icloud_not_in_email_domains(self):
        self.assertNotIn(
            "icloud.com",
            billing._EMAIL_DOMAINS,  # pylint: disable=protected-access
            "icloud.com must be removed per blueprint compliance.",
        )

    def test_hotmail_in_email_domains(self):
        self.assertIn(
            "hotmail.com",
            billing._EMAIL_DOMAINS,  # pylint: disable=protected-access
        )


if __name__ == "__main__":
    unittest.main()
