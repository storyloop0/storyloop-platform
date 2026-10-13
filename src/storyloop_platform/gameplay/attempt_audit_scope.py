"""Best-effort model execution audit around the existing turn work."""

from contextlib import contextmanager
import asyncio
import hashlib
import logging
from uuid import uuid4

from storyloop_harness.usage import collect_attempts, summarize_attempt_usage
from storyloop_platform.portal.billing import BillingPolicy
from storyloop_platform.portal.model_execution_audits import SQLModelExecutionAudits


_log = logging.getLogger(__name__)


def _warn(stage: str, error: BaseException) -> None:
    _log.warning("model execution audit %s failed: %s", stage, type(error).__name__,
                 exc_info=False)


@contextmanager
def attempt_audit_scope(repository: SQLModelExecutionAudits | None, *,
                        player_id: str, game_id: str, request_id: str,
                        player_text: str, policy: BillingPolicy | None):
    execution_id = uuid4().hex
    started = False
    if repository is not None:
        try:
            repository.start(execution_id=execution_id, player_id=player_id,
                             game_id=game_id, request_id=request_id,
                             input_hash=hashlib.sha256(player_text.encode()).hexdigest(),
                             policy=policy)
            started = True
        except BaseException as error:
            _warn("start", error)
    with collect_attempts() as collector:
        try:
            yield
        except BaseException as error:
            if started:
                _finish(repository, execution_id, collector.records,
                        "cancelled" if isinstance(error, asyncio.CancelledError) else "failed",
                        type(error).__name__)
            raise
        else:
            if started:
                _finish(repository, execution_id, collector.records, "succeeded", None)


def _finish(repository, execution_id, records, outcome, error_type):
    try:
        repository.finish(execution_id, summarize_attempt_usage(records),
                          outcome=outcome, error_type=error_type)
    except BaseException as error:
        _warn("finish", error)
