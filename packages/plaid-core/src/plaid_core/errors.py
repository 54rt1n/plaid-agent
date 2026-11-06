"""Errors safe for CLI, web and agent consumers."""


class FinanceError(Exception):
    """An operation could not complete; no provider credentials are included."""


class ProviderError(FinanceError):
    def __init__(
        self, code: str, request_id: str | None = None, *, outcome_unknown: bool | None = None
    ):
        self.code = code
        self.request_id = request_id
        self.outcome_unknown = (
            outcome_unknown
            if outcome_unknown is not None
            else code in {"NETWORK_ERROR", "INVALID_RESPONSE"}
        )
        super().__init__(f"Plaid request failed ({code})")


class ConcurrentSyncError(FinanceError):
    """Another writer changed this connection during synchronization."""
