"""Tests for mark_unconfirmed + TTL retry (watchdog timeout after submit).

Covers:
- `_FileIdempotencyStore.mark_unconfirmed` records entries with per-entry TTL.
- Unconfirmed entries are treated as duplicates (no double-charge).
- `list_unconfirmed` surfaces them for manual review.
- `clear_unconfirmed` + `mark_completed` remove them.
- Expired unconfirmed entries are evicted by `_evict_expired_task_ids`.
- Reconciliation helper with a verifier promotes / clears entries correctly.
- Persistence: unconfirmed entries survive a save/load round-trip.
- Integration: watchdog timeout AFTER submit calls `mark_unconfirmed`.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from modules.common.exceptions import SessionFlaggedError  # noqa: E402
from modules.common.types import CardInfo, WorkerTask  # noqa: E402
import integration.orchestrator as orch  # noqa: E402


def _reset_state() -> None:
    with orch._idempotency_lock:
        orch._completed_task_ids.clear()
        orch._submitted_task_ids.clear()
        orch._unconfirmed_task_ids.clear()
        orch._in_flight_task_ids.clear()
    with orch._idempotency_store_lock:
        orch._idempotency_store = None


def _make_card() -> CardInfo:
    return CardInfo(
        card_number="1" * 19, exp_month="12", exp_year="2030", cvv="123",
    )


def _make_task(task_id: str) -> WorkerTask:
    return WorkerTask(
        recipient_email="a@b.com",
        amount=10,
        primary_card=_make_card(),
        order_queue=(),
        task_id=task_id,
    )


class _IsolatedStoreTestCase(unittest.TestCase):
    """Base class that isolates the on-disk idempotency store per test."""

    def setUp(self) -> None:
        _reset_state()
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
        tmp.close()
        self._tmp_path = Path(tmp.name)
        self._tmp_path.unlink()  # Start empty so load() finds no file.
        self._orig_path = orch._IDEMPOTENCY_STORE_PATH
        orch._IDEMPOTENCY_STORE_PATH = self._tmp_path

    def tearDown(self) -> None:
        orch._IDEMPOTENCY_STORE_PATH = self._orig_path
        try:
            self._tmp_path.unlink()
        except FileNotFoundError:
            pass
        _reset_state()


class MarkUnconfirmedFileStoreTests(_IsolatedStoreTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = orch._FileIdempotencyStore()

    def test_mark_unconfirmed_records_entry(self) -> None:
        self.store.mark_unconfirmed("task-A", ttl_seconds=60)
        self.assertIn("task-A", orch._unconfirmed_task_ids)
        ts, ttl = orch._unconfirmed_task_ids["task-A"]
        self.assertAlmostEqual(ttl, 60.0, places=3)
        self.assertLessEqual(ts, time.monotonic())

    def test_mark_unconfirmed_uses_default_ttl(self) -> None:
        self.store.mark_unconfirmed("task-B")
        _, ttl = orch._unconfirmed_task_ids["task-B"]
        self.assertEqual(ttl, float(orch._UNCONFIRMED_TTL_SECONDS))

    def test_unconfirmed_is_treated_as_duplicate(self) -> None:
        self.store.mark_unconfirmed("task-C", ttl_seconds=60)
        self.assertTrue(self.store.is_duplicate("task-C"))

    def test_mark_unconfirmed_removes_submitted(self) -> None:
        self.store.mark_submitted("task-D")
        self.store.mark_unconfirmed("task-D", ttl_seconds=60)
        self.assertNotIn("task-D", orch._submitted_task_ids)
        self.assertIn("task-D", orch._unconfirmed_task_ids)

    def test_list_unconfirmed(self) -> None:
        self.store.mark_unconfirmed("t1", ttl_seconds=60)
        self.store.mark_unconfirmed("t2", ttl_seconds=60)
        self.assertEqual(sorted(self.store.list_unconfirmed()), ["t1", "t2"])

    def test_clear_unconfirmed(self) -> None:
        self.store.mark_unconfirmed("t1", ttl_seconds=60)
        self.store.clear_unconfirmed("t1")
        self.assertNotIn("t1", orch._unconfirmed_task_ids)
        self.assertEqual(self.store.list_unconfirmed(), [])

    def test_mark_completed_clears_unconfirmed(self) -> None:
        self.store.mark_unconfirmed("t1", ttl_seconds=60)
        self.store.mark_completed("t1")
        self.assertNotIn("t1", orch._unconfirmed_task_ids)
        self.assertIn("t1", orch._completed_task_ids)

    def test_expired_entry_evicted(self) -> None:
        with orch._idempotency_lock:
            orch._unconfirmed_task_ids["expired-task"] = (
                time.monotonic() - 3600.0, 10.0,
            )
        with orch._idempotency_lock:
            orch._evict_expired_task_ids()
        self.assertNotIn("expired-task", orch._unconfirmed_task_ids)

    def test_list_unconfirmed_hides_expired(self) -> None:
        with orch._idempotency_lock:
            orch._unconfirmed_task_ids["expired-task"] = (
                time.monotonic() - 3600.0, 10.0,
            )
            orch._unconfirmed_task_ids["live-task"] = (
                time.monotonic(), 3600.0,
            )
        self.assertEqual(self.store.list_unconfirmed(), ["live-task"])


class UnconfirmedPersistenceTests(_IsolatedStoreTestCase):
    def test_unconfirmed_survives_save_and_load(self) -> None:
        store = orch._FileIdempotencyStore()
        store.mark_unconfirmed("persist-task", ttl_seconds=3600)
        with orch._idempotency_lock:
            orch._unconfirmed_task_ids.clear()
        store.load()
        self.assertIn("persist-task", orch._unconfirmed_task_ids)
        _, ttl = orch._unconfirmed_task_ids["persist-task"]
        self.assertAlmostEqual(ttl, 3600.0, places=1)

    def test_persisted_file_has_unconfirmed_section(self) -> None:
        store = orch._FileIdempotencyStore()
        store.mark_unconfirmed("persist-task", ttl_seconds=3600)
        data = json.loads(self._tmp_path.read_text(encoding="utf-8"))
        self.assertIn("unconfirmed", data)
        self.assertIn("persist-task", data["unconfirmed"])
        entry = data["unconfirmed"]["persist-task"]
        self.assertIn("ts", entry)
        self.assertEqual(entry["ttl"], 3600)

    def test_expired_unconfirmed_not_reloaded(self) -> None:
        data = {
            "completed": {},
            "submitted": {},
            "unconfirmed": {
                "stale-task": {"ts": time.time() - 7200, "ttl": 60}
            },
        }
        self._tmp_path.write_text(json.dumps(data), encoding="utf-8")
        store = orch._FileIdempotencyStore()
        store.load()
        self.assertNotIn("stale-task", orch._unconfirmed_task_ids)


class ReconcileUnconfirmedTests(_IsolatedStoreTestCase):
    def test_reconcile_without_verifier_is_noop_but_reports(self) -> None:
        orch._get_idempotency_store().mark_unconfirmed("a", ttl_seconds=3600)
        orch._get_idempotency_store().mark_unconfirmed("b", ttl_seconds=3600)
        stats = orch.reconcile_unconfirmed()
        self.assertEqual(stats["checked"], 2)
        self.assertEqual(stats["confirmed"], 0)
        self.assertEqual(stats["cleared"], 0)
        self.assertEqual(stats["remaining"], 2)

    def test_reconcile_verifier_true_promotes_to_completed(self) -> None:
        store = orch._get_idempotency_store()
        store.mark_unconfirmed("a", ttl_seconds=3600)
        stats = orch.reconcile_unconfirmed(verifier=lambda tid: True)
        self.assertEqual(stats["confirmed"], 1)
        self.assertEqual(stats["cleared"], 0)
        self.assertNotIn("a", orch._unconfirmed_task_ids)
        self.assertIn("a", orch._completed_task_ids)
        self.assertEqual(stats["remaining"], 0)

    def test_reconcile_verifier_false_clears_entry(self) -> None:
        store = orch._get_idempotency_store()
        store.mark_unconfirmed("a", ttl_seconds=3600)
        stats = orch.reconcile_unconfirmed(verifier=lambda tid: False)
        self.assertEqual(stats["confirmed"], 0)
        self.assertEqual(stats["cleared"], 1)
        self.assertNotIn("a", orch._unconfirmed_task_ids)
        self.assertNotIn("a", orch._completed_task_ids)

    def test_reconcile_verifier_exception_leaves_entry(self) -> None:
        store = orch._get_idempotency_store()
        store.mark_unconfirmed("a", ttl_seconds=3600)

        def _boom(_tid: str) -> bool:
            raise RuntimeError("upstream down")

        stats = orch.reconcile_unconfirmed(verifier=_boom)
        self.assertEqual(stats["checked"], 1)
        self.assertEqual(stats["confirmed"], 0)
        self.assertEqual(stats["cleared"], 0)
        self.assertIn("a", orch._unconfirmed_task_ids)

    def test_list_unconfirmed_task_ids_helper(self) -> None:
        orch._get_idempotency_store().mark_unconfirmed("a", ttl_seconds=3600)
        self.assertIn("a", orch.list_unconfirmed_task_ids())


class WatchdogTimeoutAfterSubmitWiringTests(_IsolatedStoreTestCase):
    def test_mark_unconfirmed_called_on_watchdog_timeout_after_submit(self) -> None:
        task = _make_task("wd-after-submit")
        fake_store = MagicMock()

        with patch.object(orch, "_get_idempotency_store", return_value=fake_store), \
             patch.object(orch, "_cdp_call_with_timeout", return_value=None), \
             patch.object(orch.cdp, "_get_driver", return_value=MagicMock()), \
             patch.object(orch, "_setup_network_total_listener"), \
             patch.object(orch, "_notify_total_from_dom"), \
             patch.object(orch.billing, "select_profile", return_value=MagicMock()), \
             patch.object(orch, "_emit_billing_audit_event"), \
             patch.object(orch.watchdog, "enable_network_monitor"), \
             patch.object(orch.watchdog, "reset_session"), \
             patch.object(orch.watchdog, "wait_for_total",
                          side_effect=SessionFlaggedError("timeout")):
            with self.assertRaises(SessionFlaggedError):
                orch.run_payment_step(
                    worker_id="w1", task=task, zip_code="12345",
                )

        fake_store.mark_submitted.assert_called_once_with("wd-after-submit")
        fake_store.mark_unconfirmed.assert_called_once()
        args, kwargs = fake_store.mark_unconfirmed.call_args
        self.assertEqual(args[0], "wd-after-submit")
        self.assertEqual(kwargs.get("ttl_seconds"), orch._UNCONFIRMED_TTL_SECONDS)

    def test_mark_unconfirmed_NOT_called_when_timeout_before_submit(self) -> None:
        task = _make_task("wd-before-submit")
        fake_store = MagicMock()

        def _fail_first(fn, *a, **kw):
            raise SessionFlaggedError("preflight timeout")

        with patch.object(orch, "_get_idempotency_store", return_value=fake_store), \
             patch.object(orch, "_cdp_call_with_timeout", side_effect=_fail_first), \
             patch.object(orch.cdp, "_get_driver", return_value=MagicMock()), \
             patch.object(orch, "_setup_network_total_listener"), \
             patch.object(orch.billing, "select_profile", return_value=MagicMock()), \
             patch.object(orch, "_emit_billing_audit_event"), \
             patch.object(orch.watchdog, "enable_network_monitor"), \
             patch.object(orch.watchdog, "reset_session"):
            with self.assertRaises(SessionFlaggedError):
                orch.run_payment_step(
                    worker_id="w2", task=task, zip_code="12345",
                )

        fake_store.mark_submitted.assert_not_called()
        fake_store.mark_unconfirmed.assert_not_called()


if __name__ == "__main__":
    unittest.main()
