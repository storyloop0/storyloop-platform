"""Detached SQL snapshots of structured model execution attempts."""

from __future__ import annotations

import json
import time

from sqlalchemy import Engine, text

from storyloop_harness.usage import AttemptUsageSummary, ModelAttempt, ModelUsage, summarize_attempt_usage
from storyloop_platform.portal.attempt_costs import estimate_attempt_costs
from storyloop_platform.portal.billing import BillingPolicy


INVALID = "invalid model execution audit"


def _policy(raw: str | None) -> BillingPolicy | None:
    if raw is None:
        return None
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(INVALID)
    policy = BillingPolicy.from_dict(value, {})
    if policy is None or policy.to_dict() != value:
        raise ValueError(INVALID)
    return policy


def _decoded(row) -> dict:
    try:
        if row["schema_version"] != 1 or row["outcome"] not in ("running", "succeeded", "failed", "cancelled"):
            raise ValueError(INVALID)
        policy = _policy(row["policy_json"])
        outcome = row["outcome"]
        if outcome == "running":
            if any(row[key] is not None for key in ("error_type", "attempts_json", "summary_json")):
                raise ValueError(INVALID)
            attempts = summary = None
        else:
            if (outcome == "succeeded") != (row["error_type"] is None):
                raise ValueError(INVALID)
            if outcome != "succeeded" and (not isinstance(row["error_type"], str) or not row["error_type"]):
                raise ValueError(INVALID)
            attempts = json.loads(row["attempts_json"])
            summary = json.loads(row["summary_json"])
            if not isinstance(attempts, list) or not isinstance(summary, dict):
                raise ValueError(INVALID)
            records = [ModelAttempt(**{**item, "usage": (ModelUsage(**item["usage"])
                                                    if item["usage"] is not None else None)})
                       for item in attempts]
            computed = summarize_attempt_usage(records).to_dict()
            if computed.pop("attempts") != attempts:
                raise ValueError(INVALID)
            computed.update(estimate_attempt_costs(summarize_attempt_usage(records), policy))
            if computed != summary:
                raise ValueError(INVALID)
        return {key: row[key] for key in ("execution_id", "player_id", "game_id", "request_id",
                                           "input_hash", "schema_version", "outcome", "error_type",
                                           "created_order")} | {
            "attempts": attempts, "summary": summary,
            "policy": policy.to_dict() if policy is not None else None,
        }
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        raise ValueError(INVALID) from error


class SQLModelExecutionAudits:
    def __init__(self, engine: Engine):
        self.engine = engine

    def start(self, *, execution_id: str, player_id: str, game_id: str,
              request_id: str, input_hash: str, policy: BillingPolicy | None) -> None:
        frozen = json.dumps(policy.to_dict()) if policy is not None else None
        with self.engine.begin() as db:
            db.execute(text("""INSERT INTO model_execution_audits
                (execution_id,player_id,game_id,request_id,input_hash,schema_version,
                 outcome,policy_json,created_order)
                VALUES (:execution_id,:player_id,:game_id,:request_id,:input_hash,1,
                        'running',:policy_json,:created_order)"""), {
                "execution_id": execution_id, "player_id": player_id, "game_id": game_id,
                "request_id": request_id, "input_hash": input_hash,
                "policy_json": frozen, "created_order": time.time_ns(),
            })

    def finish(self, execution_id: str, summary: AttemptUsageSummary, *,
               outcome: str, error_type: str | None) -> None:
        if outcome not in ("succeeded", "failed", "cancelled") or (outcome == "succeeded") != (error_type is None):
            raise ValueError(INVALID)
        if outcome != "succeeded" and (not isinstance(error_type, str) or not error_type):
            raise ValueError(INVALID)
        data = summary.to_dict()
        attempts = json.dumps(data.pop("attempts"), ensure_ascii=False)
        with self.engine.begin() as db:
            row = db.execute(text("SELECT * FROM model_execution_audits WHERE execution_id=:id"),
                             {"id": execution_id}).mappings().first()
            if row is None:
                raise ValueError(INVALID)
            existing = _decoded(row)
            data.update(estimate_attempt_costs(summary, _policy(row["policy_json"])))
            stored = json.dumps(data, ensure_ascii=False)
            if existing["outcome"] != "running":
                if (existing["outcome"] != outcome or existing["error_type"] != error_type
                        or existing["attempts"] != json.loads(attempts)
                        or existing["summary"] != data):
                    raise ValueError(INVALID)
                return
            changed = db.execute(text("""UPDATE model_execution_audits
                SET outcome=:outcome,error_type=:error_type,attempts_json=:attempts,
                    summary_json=:summary WHERE execution_id=:id AND outcome='running'"""), {
                "id": execution_id, "outcome": outcome, "error_type": error_type,
                "attempts": attempts, "summary": stored,
            })
            if changed.rowcount != 1:
                raise ValueError(INVALID)

    def list_for_request(self, game_id: str, request_id: str) -> list[dict]:
        with self.engine.connect() as db:
            rows = db.execute(text("""SELECT * FROM model_execution_audits
                WHERE game_id=:game_id AND request_id=:request_id
                ORDER BY created_order,execution_id"""),
                {"game_id": game_id, "request_id": request_id}).mappings().all()
        return [_decoded(row) for row in rows]
