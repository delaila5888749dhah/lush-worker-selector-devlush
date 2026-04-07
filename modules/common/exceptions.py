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
