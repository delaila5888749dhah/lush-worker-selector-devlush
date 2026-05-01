"""End-to-end integration tests validating full Phase 1-3 wiring.

Tests run with mocked external services (no real browser, no real API calls).
"""

import sys

if "pytest" not in sys.modules and any("unittest" in arg for arg in sys.argv):
    import unittest

    raise unittest.SkipTest(
        "test_e2e_integration.py requires pytest runner; "
        "skip when collected via 'python -m unittest discover'"
    )

import os
import random
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch
import urllib.error

import pytest  # pylint: disable=import-error

from integration import orchestrator, runtime
from modules.cdp import driver as driver_mod
from modules.cdp.driver import (
    GivexDriver,
    SEL_AMOUNT_INPUT,
    SEL_GREETING_MSG,
    SEL_REVIEW_CHECKOUT,
)
from modules.cdp.fingerprint import BitBrowserSession, get_bitbrowser_client
from modules.cdp.mouse import GhostCursor, build_path
from modules.cdp.proxy import ProxyPool
from modules.common.types import BillingProfile, CardInfo, WorkerTask
from modules.common.exceptions import SelectorTimeoutError, SessionFlaggedError
from modules.delay.persona import PersonaProfile
from modules.rollout.autoscaler import AutoScaler


def _task() -> WorkerTask:
    card = CardInfo(card_number="4111111111111111", exp_month="01", exp_year="2030", cvv="123")
    return WorkerTask(
        recipient_email="recipient@example.com",
        amount=50,
        primary_card=card,
        order_queue=(card,),
        task_id="task-e2e-1",
    )


def _billing() -> BillingProfile:
    return BillingProfile(
        first_name="Jane",
        last_name="Doe",
        address="1 Main St",
        city="New York",
        state="NY",
        zip_code="10001",
        phone="2125550001",
        email="jane@example.com",
    )


def test_egift_form_uses_realistic_typing_not_send_keys(mock_webdriver):
    """fill_egift_form() must call _realistic_type_field(), never raw send_keys()."""
    mock_element = MagicMock()
    mock_webdriver.find_elements.return_value = [mock_element]
    givex = GivexDriver(mock_webdriver)
    with patch.object(driver_mod, "_random_greeting", return_value="Hi"), \
            patch.object(givex, "_realistic_type_field") as mock_type, \
            patch.object(givex, "_blur_active_field_naturally", return_value=True), \
            patch.object(givex, "_field_value_length", return_value=10), \
            patch.object(givex, "_field_value", return_value="recipient@example.com"):
        givex.fill_egift_form(_task(), _billing())
    assert mock_type.call_count == 6  # nosec B101
    mock_element.send_keys.assert_not_called()


def test_amount_field_typed_with_zero_typo_rate(mock_webdriver):
    """SEL_AMOUNT_INPUT must always be typed with typo_rate=0."""
    givex = GivexDriver(mock_webdriver)
    with patch.object(driver_mod, "_random_greeting", return_value="Hi"), \
            patch.object(givex, "_realistic_type_field") as mock_type, \
            patch.object(givex, "_blur_active_field_naturally", return_value=True), \
            patch.object(givex, "_field_value_length", return_value=10), \
            patch.object(givex, "_field_value", return_value="recipient@example.com"):
        givex.fill_egift_form(_task(), _billing())
    expected = call(SEL_AMOUNT_INPUT, "50", field_kind="amount", typo_rate=0.0)
    assert expected in mock_type.call_args_list  # nosec B101


def test_navigate_clears_browser_state_before_each_cycle(mock_webdriver):
    """navigate_to_egift() must call execute_script(localStorage.clear) and delete_all_cookies()."""
    givex = GivexDriver(mock_webdriver)
    with patch.object(givex, "find_elements", return_value=[]), \
            patch.object(givex, "_wait_for_element", return_value=True), \
            patch.object(givex, "_wait_for_url"), \
            patch.object(givex, "bounding_box_click"):
        givex.navigate_to_egift()
    assert mock_webdriver.execute_script.call_count == 1  # nosec B101
    assert mock_webdriver.delete_all_cookies.call_count == 1  # nosec B101


def test_proxy_pool_assigns_unique_proxies_per_worker(proxy_pool_3: ProxyPool):
    """3 workers → 3 unique proxies from pool, none shared."""
    assigned = [proxy_pool_3.acquire(f"worker-{i}") for i in range(3)]
    assert len(set(assigned)) == 3  # nosec B101
    assert None not in assigned  # nosec B101


def test_proxy_pool_gracefully_handles_empty_pool(proxy_pool_3: ProxyPool):
    """4th worker with pool of 3 → acquire() returns None, no exception."""
    for i in range(3):
        assert proxy_pool_3.acquire(f"worker-{i}") is not None  # nosec B101
    assert proxy_pool_3.acquire("worker-4") is None  # nosec B101


def test_proxy_pool_release_returns_to_pool(proxy_pool_3: ProxyPool):
    """release() allows proxy to be re-acquired."""
    first = proxy_pool_3.acquire("worker-1")
    proxy_pool_3.release("worker-1")
    reassigned = [proxy_pool_3.acquire(f"worker-{i}") for i in range(2, 6)]
    assert first in reassigned  # nosec B101


def test_bitbrowser_client_returns_none_when_api_key_missing():
    """get_bitbrowser_client() returns None when BITBROWSER_API_KEY not set."""
    with patch.dict(os.environ, {}, clear=True):
        assert get_bitbrowser_client() is None  # nosec B101


def test_bitbrowser_session_cleanup_does_not_propagate_errors():
    """BitBrowserSession.__exit__() suppresses cleanup errors."""
    client = MagicMock()
    client.create_profile.return_value = "profile-1"
    client.launch_profile.return_value = {"webdriver": "ws://127.0.0.1:9222/profile-1"}
    client.close_profile.side_effect = urllib.error.URLError("close failed")
    client.delete_profile.side_effect = urllib.error.URLError("delete failed")
    with BitBrowserSession(client) as (_profile_id, _webdriver_url):
        pass


def test_autoscaler_scale_down_after_consecutive_failures():
    """5 consecutive failures for same worker → _scale_down_worker called."""
    scaler = AutoScaler()
    with patch.object(scaler, "_scale_down_worker") as mock_scale:
        for _ in range(5):
            scaler.record_failure("worker-1")
    mock_scale.assert_called_once_with("worker-1")


def test_autoscaler_success_resets_consecutive_counter():
    """record_success() resets consecutive failures to 0."""
    scaler = AutoScaler()
    with patch.object(scaler, "_scale_down_worker"):
        for _ in range(3):
            scaler.record_failure("worker-1")
        scaler.record_success("worker-1")
    assert scaler.get_consecutive_failures("worker-1") == 0  # nosec B101


def test_orchestrator_calls_record_success_on_payment_complete():
    """Successful payment cycle → autoscaler.record_success() called once."""
    autoscaler = MagicMock()
    store = MagicMock()
    store.is_duplicate.return_value = False
    task = _task()
    with ExitStack() as stack:
        stack.enter_context(
            patch("integration.orchestrator._get_autoscaler", return_value=autoscaler)
        )
        stack.enter_context(
            patch("integration.orchestrator._get_idempotency_store", return_value=store)
        )
        stack.enter_context(patch("integration.orchestrator.initialize_cycle"))
        stack.enter_context(
            patch(
                "integration.orchestrator.run_payment_step",
                return_value=(SimpleNamespace(name="success"), 50.0),
            )
        )
        stack.enter_context(
            patch("integration.orchestrator.handle_outcome", return_value="complete")
        )
        stack.enter_context(patch("integration.orchestrator.cdp.unregister_driver"))
        stack.enter_context(patch("integration.orchestrator.fsm.cleanup_worker"))
        orchestrator.run_cycle(task, worker_id="worker-1")
    autoscaler.record_success.assert_called_once_with("worker-1")


def test_orchestrator_calls_record_failure_on_session_flagged():
    """SessionFlaggedError → autoscaler.record_failure() called AND exception re-raised."""
    autoscaler = MagicMock()
    store = MagicMock()
    store.is_duplicate.return_value = False
    with ExitStack() as stack:
        stack.enter_context(
            patch("integration.orchestrator._get_autoscaler", return_value=autoscaler)
        )
        stack.enter_context(
            patch("integration.orchestrator._get_idempotency_store", return_value=store)
        )
        stack.enter_context(
            patch(
                "integration.orchestrator.initialize_cycle",
                side_effect=SessionFlaggedError("flagged"),
            )
        )
        stack.enter_context(patch("integration.orchestrator.cdp.unregister_driver"))
        stack.enter_context(patch("integration.orchestrator.fsm.cleanup_worker"))
        with pytest.raises(SessionFlaggedError):
            orchestrator.run_cycle(_task(), worker_id="worker-1")
    autoscaler.record_failure.assert_called_once_with("worker-1")


def test_orchestrator_records_failure_on_review_checkout_absent_timeout():
    autoscaler = MagicMock()
    store = MagicMock()
    store.is_duplicate.return_value = False
    with ExitStack() as stack:
        stack.enter_context(patch("integration.orchestrator._get_autoscaler", return_value=autoscaler))
        stack.enter_context(patch("integration.orchestrator._get_idempotency_store", return_value=store))
        stack.enter_context(patch("integration.orchestrator.initialize_cycle", side_effect=SelectorTimeoutError(SEL_REVIEW_CHECKOUT, 21)))
        stack.enter_context(patch("integration.orchestrator.cdp.unregister_driver"))
        stack.enter_context(patch("integration.orchestrator.fsm.cleanup_worker"))
        with pytest.raises(SessionFlaggedError):
            orchestrator.run_cycle(_task(), worker_id="worker-1")
    autoscaler.record_failure.assert_called_once_with("worker-1")


def test_orchestrator_records_failure_on_review_checkout_disabled_timeout():
    autoscaler = MagicMock()
    store = MagicMock()
    store.is_duplicate.return_value = False
    err = SelectorTimeoutError(SEL_REVIEW_CHECKOUT, 21, reason="present but disabled")
    assert "present but disabled" in str(err)  # nosec B101
    with ExitStack() as stack:
        stack.enter_context(patch("integration.orchestrator._get_autoscaler", return_value=autoscaler))
        stack.enter_context(patch("integration.orchestrator._get_idempotency_store", return_value=store))
        stack.enter_context(patch("integration.orchestrator.initialize_cycle", side_effect=err))
        stack.enter_context(patch("integration.orchestrator.cdp.unregister_driver"))
        stack.enter_context(patch("integration.orchestrator.fsm.cleanup_worker"))
        with pytest.raises(SessionFlaggedError):
            orchestrator.run_cycle(_task(), worker_id="worker-1")
    autoscaler.record_failure.assert_called_once_with("worker-1")


def test_startup_config_rejects_worker_count_zero():
    """WORKER_COUNT=0 → ConfigError raised."""
    with patch.dict(os.environ, {"WORKER_COUNT": "0"}, clear=True), \
            patch("integration.runtime.validate_config", return_value=None):
        with pytest.raises(runtime.ConfigError):
            runtime._validate_startup_config()  # pylint: disable=protected-access


def test_startup_config_rejects_non_integer_worker_count():
    """WORKER_COUNT=abc → ConfigError raised."""
    with patch.dict(os.environ, {"WORKER_COUNT": "abc"}, clear=True), \
            patch("integration.runtime.validate_config", return_value=None):
        with pytest.raises(runtime.ConfigError):
            runtime._validate_startup_config()  # pylint: disable=protected-access


def test_startup_config_warns_on_missing_worker_count():
    """WORKER_COUNT not set → warning logged, no exception."""
    with patch.dict(os.environ, {}, clear=True), \
            patch("integration.runtime.validate_config", return_value=None), \
            patch("integration.runtime._logger.warning") as mock_warning:
        runtime._validate_startup_config()  # pylint: disable=protected-access
    assert any(  # nosec B101
        "WORKER_COUNT not set" in str(c.args[0])
        for c in mock_warning.call_args_list
    )


def test_ghost_cursor_never_starts_at_origin(mock_webdriver):
    """1000 GhostCursor instances → none start at (0.0, 0.0)."""
    for seed in range(1000):
        cursor = GhostCursor(mock_webdriver, random.Random(seed))
        assert cursor.position != (0.0, 0.0)  # nosec B101


def test_bezier_arc_direction_is_bidirectional():
    """100 paths → at least 30% curve left AND 30% curve right."""
    positives = 0
    negatives = 0
    for seed in range(100):
        path = build_path((0.0, 0.0), (100.0, 0.0), random.Random(seed), n_points=5)
        coord_y = path[0][1]
        if coord_y > 0:
            positives += 1
        elif coord_y < 0:
            negatives += 1
    assert positives >= 30  # nosec B101
    assert negatives >= 30  # nosec B101


def test_driver_utc_offset_flows_to_temporal_model(mock_webdriver):
    """set_proxy_utc_offset(7) → get_time_state called with 7."""
    mock_element = MagicMock()
    mock_webdriver.find_elements.return_value = [mock_element]
    mock_webdriver.execute_script.return_value = {
        "left": 1.0, "top": 1.0, "width": 10.0, "height": 10.0
    }
    givex = GivexDriver(mock_webdriver, persona=PersonaProfile(123))
    temporal = MagicMock()
    temporal.get_time_state.return_value = "DAY"
    givex._temporal = temporal  # pylint: disable=protected-access
    givex.set_proxy_utc_offset(7)
    givex.bounding_box_click(SEL_GREETING_MSG)
    temporal.get_time_state.assert_called_with(7)
