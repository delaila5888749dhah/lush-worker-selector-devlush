"""Integration-local session outcome exceptions."""

from modules.common.exceptions import SessionFlaggedError


class SessionLostError(SessionFlaggedError):
    """Raised when the CDP session is detected as lost/detached."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"session lost: reason={reason}")
