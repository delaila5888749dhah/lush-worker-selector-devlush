class SessionFlaggedError(Exception):
    pass


class CycleExhaustedError(Exception):
    pass


class InvalidStateError(Exception):
    pass


class InvalidTransitionError(Exception):
    pass
