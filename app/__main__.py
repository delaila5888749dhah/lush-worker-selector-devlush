"""Production entrypoint for lush-worker-selector.

Start with:
    python -m app

Feature flag:
    ENABLE_PRODUCTION_TASK_FN=1  (default: off)

When ``ENABLE_PRODUCTION_TASK_FN`` is **off** (the default), the runtime
starts with a no-op stub task_fn so this code can merge and coexist with
existing deployments without forcing an immediate cutover.  Set the flag
to ``1`` / ``true`` / ``yes`` to activate the production browser lifecycle.
"""
import logging

from integration import runtime

_log = logging.getLogger(__name__)  # pylint: disable=invalid-name


def _make_stub_task_fn():
    """Return a no-op task_fn used when ENABLE_PRODUCTION_TASK_FN is off."""
    def task_fn(worker_id: str) -> None:  # pylint: disable=unused-argument
        """No-op placeholder invoked for each worker cycle in stub mode."""
        _log.debug(
            "Stub task_fn called for worker %s; "
            "set ENABLE_PRODUCTION_TASK_FN=1 to enable production mode.",
            worker_id,
        )
    return task_fn


def _wire_telegram_hooks() -> None:
    """Register Telegram alert handler if TELEGRAM_ENABLED."""
    try:
        from modules.notification.telegram_notifier import register_as_alert_handler  # pylint: disable=import-outside-toplevel  # noqa: PLC0415
        register_as_alert_handler()
        _log.info("Telegram alert handler registered.")
    except Exception as exc:  # pylint: disable=broad-except
        _log.warning("Failed to register Telegram alert handler: %s", exc)


def main() -> None:
    """Parse the feature flag, select the task_fn, and start the runtime."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if runtime.is_production_task_fn_enabled():
        _log.info("ENABLE_PRODUCTION_TASK_FN=on: loading production task_fn")
        from integration.task_loader import FileTaskLoader  # pylint: disable=import-outside-toplevel  # noqa: PLC0415
        from integration.worker_task import make_task_fn  # pylint: disable=import-outside-toplevel  # noqa: PLC0415
        loader = FileTaskLoader()
        task_fn = make_task_fn(task_source=loader.get_task)
        _wire_telegram_hooks()
    else:
        _log.info(
            "ENABLE_PRODUCTION_TASK_FN is off; using no-op stub task_fn. "
            "Set ENABLE_PRODUCTION_TASK_FN=1 to enable production mode."
        )
        task_fn = _make_stub_task_fn()
    runtime.start(task_fn)


if __name__ == "__main__":
    main()
