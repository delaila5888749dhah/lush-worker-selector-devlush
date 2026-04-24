class SessionFlaggedError(Exception):
    pass


class CycleExhaustedError(Exception):
    pass


class InvalidStateError(Exception):
    pass


class InvalidTransitionError(Exception):
    pass


class SelectorTimeoutError(SessionFlaggedError):
    """Raised when a CDP/Selenium selector does not appear within timeout.

    Inherits from SessionFlaggedError so the runtime's existing exception
    handler treats it as a flagged session (exits cycle gracefully).

    Attributes:
        selector: The CSS selector or locator that timed out.
        timeout: The timeout in seconds that was exceeded.
    """
    def __init__(self, selector: str, timeout: float):
        self.selector = selector
        self.timeout = timeout
        super().__init__(
            f"Selector '{selector}' not found within {timeout}s"
        )


class PageStateError(SessionFlaggedError):
    """Raised when detect_page_state() cannot determine a known state.

    Inherits from SessionFlaggedError so the runtime treats it as a
    cycle failure and moves to the next attempt.

    Attributes:
        detected: The raw string returned by the page detection logic.
    """
    def __init__(self, detected: str):
        self.detected = detected
        super().__init__(
            f"Cannot map page state '{detected}' to a known FSM state"
        )


class CDPError(Exception):
    """Raised when a CDP operation fails in a way that must abort the cycle.

    Used to propagate failures from low-level CDP helpers (e.g.
    :meth:`modules.cdp.driver.GivexDriver.clear_card_fields_cdp`) so that the
    orchestrator retry loop can mark the cycle as failed and **not** submit
    again. Swallowing such errors would risk double-charging (P1-4).
    """


class CDPClickError(CDPError):
    """Raised when :meth:`bounding_box_click` cannot dispatch a CDP click.

    In strict mode (the default for :class:`~modules.cdp.driver.GivexDriver`),
    any failure path in ``bounding_box_click`` — rect fetch, zero-size rect,
    missing persona RNG, or CDP dispatch failure — raises this exception
    instead of silently falling back to a Selenium-native ``.click()``, which
    would emit ``isTrusted=False`` mouse events and defeat anti-detect.
    """


class CDPCommandError(SessionFlaggedError):
    """Raised when a CDP command fails in a non-retryable manner.

    Inherits from SessionFlaggedError so the runtime treats it as a
    flagged session requiring cleanup rather than a transient retry.

    Attributes:
        command: The CDP method name that failed (e.g. ``"Input.dispatchMouseEvent"``).
        detail: Sanitized error description (PII already redacted by caller).
    """
    def __init__(self, command: str, detail: str):
        self.command = command
        self.detail = detail
        super().__init__(
            f"CDP command '{command}' failed: {detail}"
        )
