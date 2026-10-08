class PAError(Exception):
    pass


class NotFound(PAError):
    pass


class IllegalTransition(PAError):
    pass


class Forbidden(PAError):
    pass


class StaleApproval(PAError):
    pass


class ScopeChanged(PAError):
    pass


class OutputBlocked(PAError):
    pass
