"""Turn failure metadata must not guess whether an action was committed."""

from storyloop_harness.context import ContextBudgetExceeded
from storyloop_harness.generation import (
    ExecutionRequestLimitExceeded, ExecutionTimeout,
    GenerationRequestLimitExceeded, GenerationTimeout,
)


class TurnInputError(ValueError):
    """Input rejected before any turn work or world mutation begins."""


class TurnRecoveryRequired(ValueError):
    """World work exists without sufficient evidence to settle it safely."""


class TurnGenerationUncertain(TurnRecoveryRequired):
    """A durable generation claim has no qualified result."""

    def __init__(self) -> None:
        super().__init__("这条行动的生成结果暂时无法确认，已停止重试。请保留此请求并联系管理员。")


def turn_failure(error: Exception, request_id: str | None, *,
                 message: str | None = None, code: str = "TURN_FAILED") -> dict:
    if isinstance(error, TurnGenerationUncertain):
        return {"code": "GENERATION_UNCERTAIN", "message": str(error),
                "retryable": False, "commit_state": "unknown", "request_id": request_id}
    if isinstance(error, ExecutionRequestLimitExceeded):
        return {
            "code": "EXECUTION_REQUEST_LIMIT_EXCEEDED",
            "message": "Turn execution request limit exceeded",
            "retryable": False,
            "commit_state": "unknown",
            "request_id": request_id,
        }
    if isinstance(error, ExecutionTimeout):
        return {
            "code": "EXECUTION_TIMEOUT",
            "message": "Turn execution timeout exceeded",
            "retryable": False,
            "commit_state": "unknown",
            "request_id": request_id,
        }
    if isinstance(error, GenerationRequestLimitExceeded):
        return {
            "code": "GENERATION_REQUEST_LIMIT_EXCEEDED",
            "message": "Structured generation request limit exceeded",
            "retryable": False,
            "commit_state": "unknown",
            "request_id": request_id,
        }
    if isinstance(error, GenerationTimeout):
        return {
            "code": "GENERATION_TIMEOUT",
            "message": "Structured generation timeout exceeded",
            "retryable": False,
            "commit_state": "unknown",
            "request_id": request_id,
        }
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
