"""Phase 1 — Runtime / Env / Docs bringup required tests.

Covers:

* RT-ENV-DOCS — ``.env.example`` documents all listed runtime env vars.
* RT-STAGGER-FLAG — stagger flag is independent of the behavior-delay flag.
* RT-CAP-50-VS-500 — cap enforcement is consistent between
  :func:`modules.rollout.main.configure_max_workers` and
  :func:`integration.runtime._validate_startup_config`.
"""
from __future__ import annotations

import os
import re
import unittest
from pathlib import Path
from unittest import mock

from integration import runtime
from integration.runtime import ConfigError
from modules.rollout import main as rollout_module


REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_EXAMPLE = REPO_ROOT / ".env.example"
_ENV_SOURCE_FILES = (
    REPO_ROOT / "integration/runtime.py",
    REPO_ROOT / "modules/rollout/main.py",
    REPO_ROOT / "modules/delay/config.py",
    REPO_ROOT / "modules/billing/main.py",
    REPO_ROOT / "modules/cdp/fingerprint.py",
    REPO_ROOT / "modules/cdp/driver.py",
)

# All runtime env vars the Phase 1 ticket requires to be documented.
_REQUIRED_ENV_VARS = (
    "MAX_WORKER_COUNT",
    "WORKER_COUNT",
    "BILLING_CB_THRESHOLD",
    "BILLING_CB_PAUSE",
    "ENABLE_PRODUCTION_TASK_FN",
    "DELAY_MAX_TYPING_DELAY",
    "DELAY_MAX_HESITATION_DELAY",
    "DELAY_MAX_STEP_DELAY",
    "DELAY_WATCHDOG_HEADROOM",
    "PAYMENT_WATCHDOG_TIMEOUT_S",
    "BITBROWSER_POOL_MODE",
    "BITBROWSER_PROFILE_IDS",
    "GEOIP_DB_PATH",
    "BILLING_POOL_DIR",
    "POPUP_USE_XPATH",
    "POPUP_CLOSE_MAX_RETRIES",
)


def _grep_derived_env_vars() -> set[str]:
    """Return the literal env var names grep would find in the review files."""
    pattern = re.compile(
        r"""os\.(?:environ\.get|getenv)\(\s*['"]([^'"]+)['"]"""
    )
    names: set[str] = set()
    for path in _ENV_SOURCE_FILES:
        names.update(pattern.findall(path.read_text(encoding="utf-8")))
    return names


class TestEnvExampleDocumentsAllRuntimeEnvVars(unittest.TestCase):
    """RT-ENV-DOCS — parse .env.example, assert all listed vars present."""

    def test_env_example_documents_all_runtime_env_vars(self):
        self.assertTrue(
            ENV_EXAMPLE.is_file(),
            f".env.example missing at {ENV_EXAMPLE}",
        )
        content = ENV_EXAMPLE.read_text(encoding="utf-8")
        documented = _REQUIRED_ENV_VARS + tuple(sorted(_grep_derived_env_vars()))
        # Accept either `NAME=...` (live default) or `# NAME=...` (commented
        # default) — both count as "documented" since operators see the knob.
        for name in documented:
            with self.subTest(var=name):
                pattern = re.compile(
                    rf"^[#\s]*{re.escape(name)}=", re.MULTILINE
                )
                self.assertRegex(
                    content,
                    pattern,
                    f".env.example does not document {name!r}",
                )


class TestStaggerEnabledIndependentOfBehaviorDelay(unittest.TestCase):
    """RT-STAGGER-FLAG — stagger is applied even when behavior delay is off."""

    def setUp(self):
        # Avoid leaking module-level flags between tests.
        runtime.set_behavior_delay_enabled(True)
        runtime.set_stagger_enabled(True)
        with runtime._stagger_lock:  # pylint: disable=protected-access
            runtime._last_worker_launch_ts = 0.0  # pylint: disable=protected-access
        runtime._stop_event.clear()  # pylint: disable=protected-access

    def tearDown(self):
        runtime.set_behavior_delay_enabled(True)
        runtime.set_stagger_enabled(True)

    def test_stagger_enabled_independent_of_behavior_delay(self):
        """Disable behavior delay; stagger must still run in _apply_scale()."""
        runtime.set_behavior_delay_enabled(False)
        runtime.set_stagger_enabled(True)

        launches: list[object] = []

        def _fake_start(_task_fn):
            launches.append(None)

        # Force the scale path to exercise multiple launches.
        with runtime._lock:  # pylint: disable=protected-access
            runtime._state = "RUNNING"  # pylint: disable=protected-access
        try:
            with mock.patch.object(
                runtime, "_stagger_sleep_before_launch", return_value=0.0
            ) as m_stagger, mock.patch.object(
                runtime, "start_worker", side_effect=_fake_start
            ):
                runtime._apply_scale(3, lambda _: None)  # pylint: disable=protected-access
            # Three launches from an empty worker set → every launch goes
            # through the stagger helper even though behavior delay is off.
            self.assertEqual(len(launches), 3)
            self.assertEqual(m_stagger.call_count, 3)
        finally:
            with runtime._lock:  # pylint: disable=protected-access
                runtime._state = "INIT"  # pylint: disable=protected-access

    def test_stagger_helper_ignores_behavior_delay_flag(self):
        """The helper itself must only consult _stagger_enabled."""
        runtime.set_behavior_delay_enabled(False)
        runtime.set_stagger_enabled(False)
        with mock.patch.object(runtime._stop_event, "wait") as m_wait:  # pylint: disable=protected-access
            self.assertEqual(runtime._stagger_sleep_before_launch(), 0.0)  # pylint: disable=protected-access
        m_wait.assert_not_called()

    def test_stagger_disabled_skips_sleep_helper(self):
        """Sanity: the new flag gates the stagger path, not behavior delay."""
        runtime.set_behavior_delay_enabled(True)
        runtime.set_stagger_enabled(False)

        with runtime._lock:  # pylint: disable=protected-access
            runtime._state = "RUNNING"  # pylint: disable=protected-access
        try:
            with mock.patch.object(
                runtime, "_stagger_sleep_before_launch", return_value=0.0
            ) as m_stagger, mock.patch.object(runtime, "start_worker"):
                runtime._apply_scale(3, lambda _: None)  # pylint: disable=protected-access
            m_stagger.assert_not_called()
        finally:
            with runtime._lock:  # pylint: disable=protected-access
                runtime._state = "INIT"  # pylint: disable=protected-access


class TestMaxWorkerCountCapConsistent(unittest.TestCase):
    """RT-CAP-50-VS-500 — cap is enforced identically at both entry points."""

    def setUp(self):
        self._saved_max = os.environ.get("MAX_WORKER_COUNT")
        self._saved_workers = os.environ.get("WORKER_COUNT")

    def tearDown(self):
        if self._saved_max is None:
            os.environ.pop("MAX_WORKER_COUNT", None)
        else:
            os.environ["MAX_WORKER_COUNT"] = self._saved_max
        if self._saved_workers is None:
            os.environ.pop("WORKER_COUNT", None)
        else:
            os.environ["WORKER_COUNT"] = self._saved_workers

    def test_max_worker_count_cap_consistent_between_configure_and_startup(self):
        """Both the rollout configure helper and startup validation agree on
        the accepted ``[1, N]`` range — whatever N happens to be today.
        """
        cap = rollout_module._MAX_MAX_WORKER_COUNT  # pylint: disable=protected-access
        # Phase 1 decision (option a): raise cap to 500.
        self.assertEqual(cap, 500)

        # Accepted boundary values pass both paths.
        for good in (1, cap):
            with self.subTest(value=good, path="configure"):
                # Must not raise.
                rollout_module.configure_max_workers(good)
            with self.subTest(value=good, path="startup"):
                os.environ["MAX_WORKER_COUNT"] = str(good)
                os.environ.pop("WORKER_COUNT", None)
                runtime._validate_startup_config()  # pylint: disable=protected-access

        # Just-above-cap values fail both paths.
        over = cap + 1
        with self.subTest(value=over, path="configure"):
            with self.assertRaises(ValueError):
                rollout_module.configure_max_workers(over)
        with self.subTest(value=over, path="startup"):
            os.environ["MAX_WORKER_COUNT"] = str(over)
            os.environ.pop("WORKER_COUNT", None)
            with self.assertRaises(ConfigError):
                runtime._validate_startup_config()  # pylint: disable=protected-access

        # Zero is rejected by both paths.
        with self.subTest(value=0, path="configure"):
            with self.assertRaises(ValueError):
                rollout_module.configure_max_workers(0)
        with self.subTest(value=0, path="startup"):
            os.environ["MAX_WORKER_COUNT"] = "0"
            os.environ.pop("WORKER_COUNT", None)
            with self.assertRaises(ConfigError):
                runtime._validate_startup_config()  # pylint: disable=protected-access

    def test_build_scale_steps_reaches_blueprint_500_example(self):
        """The 500-cap progression must match the Blueprint decade series."""
        self.assertEqual(
            rollout_module._build_scale_steps(500),  # pylint: disable=protected-access
            (1, 3, 5, 10, 20, 50, 100, 200, 500),
        )

    def test_startup_warns_when_max_worker_count_exceeds_100(self):
        """Option (a) contract: values >100 are legal but must log a warning."""
        os.environ["MAX_WORKER_COUNT"] = "250"
        os.environ.pop("WORKER_COUNT", None)
        with mock.patch.object(runtime, "_logger") as m_logger:
            runtime._validate_startup_config()  # pylint: disable=protected-access
        self.assertTrue(
            any(
                "MAX_WORKER_COUNT" in str(call.args[0])
                and "exceeds 100" in str(call.args[0])
                for call in m_logger.warning.call_args_list
            ),
            f"expected high-concurrency warning, got "
            f"{m_logger.warning.call_args_list!r}",
        )

    def test_startup_silent_when_max_worker_count_at_or_below_100(self):
        """Values ≤100 must not emit the high-concurrency warning."""
        os.environ["MAX_WORKER_COUNT"] = "100"
        os.environ.pop("WORKER_COUNT", None)
        with mock.patch.object(runtime, "_logger") as m_logger:
            runtime._validate_startup_config()  # pylint: disable=protected-access
        self.assertFalse(
            any(
                "exceeds 100" in str(call.args[0])
                for call in m_logger.warning.call_args_list
            )
        )


if __name__ == "__main__":
    unittest.main()
