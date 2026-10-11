"""Turn failure metadata must not guess whether an action was committed."""

from storyloop_harness.context import ContextBudgetExceeded


class TurnInputError(ValueError):
    """Input rejected before any turn work or world mutation begins."""


class TurnRecoveryRequired(ValueError):
    """World work exists without sufficient evidence to settle it safely."""


def turn_failure(error: Exception, request_id: str | None, *,
                 message: str | None = None, code: str = "TURN_FAILED") -> dict:
    unstarted = isinstance(error, TurnInputError)
    recovery_required = isinstance(error, TurnRecoveryRequired)
    budget_exceeded = isinstance(error, ContextBudgetExceeded)
    return {"code": ("INVALID_TURN_INPUT" if unstarted else
                     "TURN_RECOVERY_REQUIRED" if recovery_required else
                     "CONTEXT_BUDGET_EXCEEDED" if budget_exceeded else code),
            "message": ("Estimated ordinary scene request exceeds the configured context budget"
                        if budget_exceeded else message if message is not None else str(error)),
            "retryable": not (unstarted or recovery_required or budget_exceeded),
            "commit_state": "not_started" if unstarted else "unknown",
            "request_id": request_id}
