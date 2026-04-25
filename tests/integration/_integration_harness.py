"""Shared helpers for L3 integration harness and L4 live-smoke tests (F-09).

Imported by tests/integration/test_l3_harness.py and
tests/integration/test_l4_smoke.py.  Placed under tests/ (not
tests/integration/) so that there is no package-naming conflict with the
project-root integration/ package.

Provides
--------
  _StubGivexDriver   — minimal stub driver that records method calls and
                       injects configurable FSM state transitions and errors.
  _make_task         — factory for a fully-populated WorkerTask.
  _make_billing_profile — factory for a fully-populated BillingProfile.
  _IntegrationBase   — setUp/tearDown mixin for integration test cases.
  FakeBitBrowserServer — lightweight fake BitBrowser HTTP server.
  make_mock_billing  — builds a MagicMock billing module.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple
from unittest.mock import MagicMock, patch

from modules.common.exceptions import (
    InvalidStateError,
    InvalidTransitionError,
    SessionFlaggedError,
)
from modules.common.types import BillingProfile, CardInfo, WorkerTask
import modules.cdp.main as _cdp_main
from modules.fsm.main import cleanup_worker, transition_for_worker
from integration.orchestrator import (
    _completed_task_ids,
    _idempotency_lock,
    _in_flight_task_ids,
    _network_listener_lock,
    _notified_workers_this_cycle,
    _submitted_task_ids,
)
from modules.watchdog.main import reset as _reset_watchdog


# ── Domain helpers ─────────────────────────────────────────────────────────────

def _make_task(
    task_id: str = "l3-task-001",
    amount: int = 50,
    recipient: str = "recipient@example.test",
) -> WorkerTask:
    card = CardInfo(
        card_number="4111111111111111",
        exp_month="01",
        exp_year="2030",
        cvv="123",
    )
    return WorkerTask(
        task_id=task_id,
        recipient_email=recipient,
        amount=amount,
        primary_card=card,
        order_queue=(card,),
    )


def _make_billing_profile(
    zip_code: str = "10001",
    email: str = "billing@example.test",
) -> BillingProfile:
    return BillingProfile(
        first_name="Integration",
        last_name="Tester",
        address="1 Test Ave",
        city="New York",
        state="NY",
        zip_code=zip_code,
        phone="2125550199",
        email=email,
    )


def _action_name(action):
    return action[0] if isinstance(action, tuple) else action


# ── Stub GivexDriver ───────────────────────────────────────────────────────────

class _StubGivexDriver:
    """Minimal Givex driver stub for integration tests.

    Records all method invocations in ``self.calls`` and supports:
    - Configurable FSM state transition on ``submit_purchase()`` (``final_state``).
    - Error injection at a named method via ``error_at``.
    - Configurable DOM total for watchdog notification (``dom_total``).
    - Optional ``add_cdp_listener`` for CDP body path tests (``enable_cdp_listener``).

    Method signatures intentionally mirror the real ``GivexDriver`` interface;
    parameters that the stub does not consume are kept under their canonical
    names for documentation value (hence the unused-argument disable below).
    """
    # pylint: disable=unused-argument

    def __init__(  # pylint: disable=too-many-arguments
        self,
        worker_id: str,
        final_state: str = "success",
        error_at: Optional[str] = None,
        dom_total: Optional[str] = "50.00",
        enable_cdp_listener: bool = False,
    ) -> None:
        self.worker_id = worker_id
        self.final_state = final_state
        self.error_at = error_at
        self.dom_total = dom_total
        self.calls: List[str] = []
        self._cdp_listeners: Dict[str, Callable] = {}
        if enable_cdp_listener:
            def _add_listener(event, callback):
                self._cdp_listeners[event] = callback
            self.add_cdp_listener = _add_listener

    def _record(self, name: str) -> None:
        self.calls.append(name)
        if self.error_at == name:
            raise SessionFlaggedError(f"Injected error at stub method '{name}'")

    # ── Purchase sequence methods ──────────────────────────────────────────────

    def preflight_geo_check(self) -> str:
        self._record("preflight_geo_check")
        return "US"

    def navigate_to_egift(self) -> None:
        self._record("navigate_to_egift")

    def fill_egift_form(self, task, profile) -> None:
        self._record("fill_egift_form")

    def add_to_cart_and_checkout(self) -> None:
        self._record("add_to_cart_and_checkout")

    def select_guest_checkout(self, email: str) -> None:
        self._record("select_guest_checkout")

    def fill_payment_and_billing(self, card, profile) -> None:
        self._record("fill_payment_and_billing")

    def submit_purchase(self) -> None:
        self._record("submit_purchase")
        # Transition FSM to simulate real page state detection.
        if self.final_state:
            try:
                transition_for_worker(self.worker_id, self.final_state)
            except (InvalidStateError, InvalidTransitionError, ValueError):
                # FSM transition may legitimately fail when the stub is exercised
                # outside a full run_cycle (e.g. direct run_payment_step calls where
                # the worker registry entry may not be initialized). This is expected
                # stub behaviour and should not propagate to the test.
                pass

    def run_full_cycle(self, task, profile) -> str:
        self._record("run_full_cycle")
        return self.final_state

    def clear_card_fields(self) -> None:
        self._record("clear_card_fields")

    # ── Selenium / CDP passthrough methods ─────────────────────────────────────

    def execute_script(self, script: str) -> object:
        """Return configurable DOM total string for watchdog notification."""
        return self.dom_total

    def execute_cdp_cmd(self, cmd: str, params: Optional[dict] = None) -> object:
        """Respond to Network.getResponseBody with a JSON body containing total."""
        if cmd == "Network.getResponseBody":
            return {
                "body": json.dumps({"total": 50.0}),
                "base64Encoded": False,
            }
        return None

    def fire_cdp_response(
        self,
        url: str = "/checkout/total",
        request_id: str = "req-stub-001",
    ) -> None:
        """Manually fire the CDP Network.responseReceived callback."""
        cb = self._cdp_listeners.get("Network.responseReceived")
        if cb:
            cb({"requestId": request_id, "response": {"url": url}})


# ── Integration test base class ────────────────────────────────────────────────

class _IntegrationBase:
    """Mixin that provides setUp/tearDown for integration test cases.

    Subclasses must also inherit from ``unittest.TestCase`` and may override
    ``worker_id`` (default ``"l3-worker"``) before calling ``super().setUp()``.

    setUp clears idempotency store, FSM state, and watchdog registry.
    tearDown unregisters the CDP driver and cleans up FSM state.
    """

    worker_id: str = "l3-worker"

    def setUp(self):  # pylint: disable=invalid-name
        with _idempotency_lock:
            _completed_task_ids.clear()
            _in_flight_task_ids.clear()
            _submitted_task_ids.clear()
        with _network_listener_lock:
            _notified_workers_this_cycle.discard(self.worker_id)
        _reset_watchdog()
        cleanup_worker(self.worker_id)
        # Phase 3A introduced a Phase A pre-fill ``wait_for_total`` in
        # ``run_payment_step`` that fires BEFORE any browser network event
        # can deliver a checkout total in stub-driven integration tests.
        # Route ``modules.watchdog.main.wait_for_total`` straight to the
        # registered stub driver's ``dom_total`` attribute so that
        # Phase A / Phase C succeed (or time out) deterministically based
        # on the stub configuration:
        #   - dom_total non-None → return float(dom_total)
        #   - dom_total is None  → raise SessionFlaggedError(timeout)
        # Tests that need finer control over Phase A vs Phase C (e.g.
        # "Phase A succeeds, Phase C times out") can override this patch
        # with their own ``patch(..., side_effect=[...])``.
        self._wait_for_total_patcher = patch(
            "modules.watchdog.main.wait_for_total",
            side_effect=self._stub_wait_for_total,
        )
        self._wait_for_total_patcher.start()

    def tearDown(self):  # pylint: disable=invalid-name
        if hasattr(self, "_wait_for_total_patcher"):
            try:
                self._wait_for_total_patcher.stop()
            except RuntimeError:
                # Test already stopped/replaced the patcher — no-op.
                pass
        _cdp_main.unregister_driver(self.worker_id)
        cleanup_worker(self.worker_id)

    def _stub_wait_for_total(self, worker_id, timeout=None):
        """Resolve wait_for_total against the registered stub driver's dom_total.

        Acts as the single source of truth for the watchdog total during
        integration tests, replacing the CDP-listener / DOM-fallback path.
        """
        driver = _cdp_main._get_driver(worker_id)  # pylint: disable=protected-access
        dom_total = getattr(driver, "dom_total", None) if driver is not None else None
        if dom_total is None:
            raise SessionFlaggedError(
                f"Timeout ({timeout}s) waiting for total amount for worker '{worker_id}'"
            )
        try:
            return float(dom_total)
        except (TypeError, ValueError) as exc:
            raise SessionFlaggedError(
                f"Invalid stub dom_total={dom_total!r} for worker '{worker_id}': {exc}"
            ) from exc


# ── Fake BitBrowser HTTP server ────────────────────────────────────────────────

class _FakeBitBrowserHandler(BaseHTTPRequestHandler):
    """BitBrowser API stub implementing create/open/close/delete endpoints."""

    _lock = threading.Lock()
    _calls: List[Tuple[str, Dict[str, Any]]] = []

    @classmethod
    def reset(cls) -> None:
        with cls._lock:
            cls._calls = []

    @classmethod
    def snapshot(cls) -> list:
        with cls._lock:
            return list(cls._calls)

    def _send_json(self, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # pylint: disable=invalid-name
        self._send_json({"data": []})

    def do_POST(self):  # pylint: disable=invalid-name
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        with self._lock:
            self._calls.append((self.path, payload))
        if self.path == "/api/v1/browser/create":
            self._send_json({"data": {"id": "integration-profile-1"}})
        elif self.path == "/api/v1/browser/open":
            self._send_json(
                {"data": {"webdriver": "ws://127.0.0.1:9222/integration-profile-1"}}
            )
        else:
            self._send_json({"ok": True})

    def log_message(self, fmt, *args):  # pylint: disable=arguments-differ
        pass  # suppress server logs in test output


class FakeBitBrowserServer:
    """Context manager that starts a local fake BitBrowser HTTP server.

    Usage::

        with FakeBitBrowserServer() as srv:
            # srv.endpoint  → "http://127.0.0.1:<port>"
            # srv.calls()   → list of (path, payload) tuples
            ...
    """

    def __init__(self) -> None:
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.endpoint: str = ""

    def __enter__(self) -> "FakeBitBrowserServer":
        _FakeBitBrowserHandler.reset()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeBitBrowserHandler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        host, port = self._server.server_address
        self.endpoint = f"http://{host}:{port}"
        return self

    def __exit__(self, *_) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
        if self._thread:
            self._thread.join(timeout=5)

    def calls(self) -> list:
        return _FakeBitBrowserHandler.snapshot()


# ── Mock billing helper ────────────────────────────────────────────────────────

def make_mock_billing(profile: Optional[BillingProfile] = None) -> MagicMock:
    """Return a MagicMock billing module with ``select_profile`` pre-wired."""
    mock = MagicMock()
    mock.select_profile.return_value = profile or _make_billing_profile()
    return mock
