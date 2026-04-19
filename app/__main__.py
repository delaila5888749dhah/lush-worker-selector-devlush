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
import os
import sys

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
    except Exception as exc:  # pylint: disable=broad-except
        _log.warning("Failed to register Telegram alert handler: %s", exc)


def _startup_check_geoip() -> None:
    """Verify the MaxMind GeoLite2 database is present and initialise the reader.

    In production mode (``ENABLE_PRODUCTION_TASK_FN=1``):
      - If the ``.mmdb`` file is missing, startup is **aborted** with a clear
        error message instructing the operator to run
        ``scripts/download_maxmind.py``.

    In stub/dev mode (flag off):
      - If the file is missing, a warning is logged and startup continues.
      - The MaxMind reader singleton is not initialised (lookups fall back to
        per-call lazy mode or return ``None``).
    """
    from modules.cdp.driver import init_maxmind_reader  # noqa: PLC0415
    mmdb_path = os.environ.get("GEOIP_DB_PATH", "data/GeoLite2-City.mmdb")
    is_production = runtime.is_production_task_fn_enabled()

    if not os.path.exists(mmdb_path):
        if is_production:
            _log.critical(
                "STARTUP ABORTED: MaxMind GeoLite2 database not found at '%s'. "
                "Run scripts/download_maxmind.py (requires MAXMIND_LICENSE_KEY) "
                "to download the database, then restart.",
                mmdb_path,
            )
            sys.exit(1)
        _log.warning(
            "MaxMind GeoLite2 database not found at '%s'. "
            "IP → zip lookups will be unavailable. "
            "Run scripts/download_maxmind.py to enable offline geo lookups.",
            mmdb_path,
        )
        return

    try:
        init_maxmind_reader(mmdb_path)
    except Exception as exc:  # pylint: disable=broad-except
        if is_production:
            _log.critical(
                "STARTUP ABORTED: Failed to initialise MaxMind reader at '%s': %s. "
                "Ensure the database file is valid and re-run "
                "scripts/download_maxmind.py if necessary.",
                mmdb_path, exc,
            )
            sys.exit(1)
        _log.warning(
            "MaxMind reader initialisation failed at '%s': %s. "
            "Continuing in stub mode — zip lookups unavailable.",
            mmdb_path, exc,
        )


def _startup_load_billing_pool() -> None:
    """Eagerly load the billing pool at startup and log the profile count."""
    try:
        from modules.billing import main as billing  # noqa: PLC0415
        count = billing.load_billing_pool()
        _log.info("Billing pool loaded at startup: %d profiles.", count)
    except Exception as exc:  # pylint: disable=broad-except
        _log.warning("Billing pool startup load failed: %s. Pool will be loaded lazily.", exc)


def main() -> None:
    """Parse the feature flag, select the task_fn, and start the runtime."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    # ── Startup checks ────────────────────────────────────────────────────────
    _startup_check_geoip()      # P1/P2: MaxMind DB presence check + singleton init
    _startup_load_billing_pool()  # P6: Eager billing pool load

    if runtime.is_production_task_fn_enabled():
        _log.info("ENABLE_PRODUCTION_TASK_FN=on: loading production task_fn")
        from integration.task_loader import FileTaskLoader  # pylint: disable=import-outside-toplevel  # noqa: PLC0415
        from integration.worker_task import make_task_fn  # pylint: disable=import-outside-toplevel  # noqa: PLC0415
        task_fn = make_task_fn(task_source=FileTaskLoader().get_task)
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
