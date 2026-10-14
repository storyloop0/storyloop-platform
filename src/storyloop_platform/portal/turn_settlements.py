"""Immutable settlement inputs; this is not a journal of unfinished model calls."""

from dataclasses import dataclass
import hashlib
import json
import re
import time

from sqlalchemy import Engine, text

from storyloop_platform.portal.billing import BillingPolicy, ModelUsage
from storyloop_platform.portal.turn_errors import TurnRecoveryRequired


@dataclass(frozen=True)
class TurnSettlement:
    response: dict
    usage: list[ModelUsage]
    policy: BillingPolicy


class SQLTurnSettlements:
    def __init__(self, engine: Engine):
        self.engine = engine

    def get(self, game_id: str, request_id: str, player_text: str) -> TurnSettlement | None:
        return self.get_by_hash(game_id, request_id,
                                hashlib.sha256(player_text.encode()).hexdigest())

    def get_by_hash(self, game_id: str, request_id: str,
                    input_hash: str) -> TurnSettlement | None:
        if type(input_hash) is not str or not re.fullmatch(r"[0-9a-f]{64}", input_hash):
            raise ValueError("invalid input hash")
        with self.engine.connect() as db:
            row = db.execute(text("""SELECT * FROM turn_settlement_intents
                WHERE game_id=:game AND request_id=:request"""),
                {"game": game_id, "request": request_id}).mappings().first()
        if row is None:
            return None
        if row["input_hash"] != input_hash:
            raise ValueError("request ID already belongs to different input")
        if row["schema_version"] != 1:
            raise TurnRecoveryRequired("回合计量记录格式不受支持，请联系管理员恢复。")
        try:
            policy = BillingPolicy.from_dict(json.loads(row["policy_json"]), {})
            if policy is None:
                raise ValueError("missing pricing")
            response = json.loads(row["response_json"])
            if not isinstance(response, dict):
                raise ValueError("invalid response")
            usage = [ModelUsage(**item) for item in json.loads(row["usage_json"])]
            policy.price_milli_points(usage)
        except (ValueError, TypeError, KeyError) as error:
            raise TurnRecoveryRequired("回合计量记录不完整，请联系管理员恢复。") from error
        return TurnSettlement(response, usage, policy)

    def prepare(self, game_id: str, request_id: str, player_text: str,
                response: dict, usage: list[ModelUsage], policy: BillingPolicy) -> TurnSettlement:
        with self.engine.begin() as db:
            db.execute(text("""INSERT INTO turn_settlement_intents
                (game_id,request_id,input_hash,schema_version,response_json,usage_json,policy_json,created_order)
                VALUES (:game,:request,:hash,1,:response,:usage,:policy,:created)
                ON CONFLICT(game_id,request_id) DO NOTHING"""), {
                    "game": game_id, "request": request_id,
                    "hash": hashlib.sha256(player_text.encode()).hexdigest(),
                    "response": json.dumps(response, ensure_ascii=False),
                    "usage": json.dumps([item.to_dict() for item in usage]),
                    "policy": json.dumps(policy.to_dict()), "created": time.time_ns(),
                })
        prepared = self.get(game_id, request_id, player_text)
        if prepared is None:
            raise RuntimeError("turn settlement was not persisted")
        return prepared
