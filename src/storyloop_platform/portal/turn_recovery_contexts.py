"""Immutable SQL context for recovery of ordinary player scene generation."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re

from sqlalchemy import Engine, text

from storyloop_harness.advanced import Snapshot, snapshot_state_fingerprint
from storyloop_platform.portal.billing import BillingPolicy
from storyloop_platform.portal.turn_errors import TurnRecoveryRequired


class TurnRecoveryContextError(TurnRecoveryRequired):
    """Stored recovery evidence is absent, conflicting, or malformed."""


_HEX = re.compile(r"[0-9a-f]{64}\Z")
_REQUEST = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_FIELDS = frozenset({"format_version", "scope", "player_id", "game_id", "request_id",
                     "turn_id", "input_hash", "generation_identity", "billing_enabled",
                     "policy", "base_version", "base_tick", "base_state_hash",
                     "base_visible_observation_ids", "created_order"})
_RATE_FIELDS = frozenset({"input_rmb_per_million", "output_rmb_per_million",
                          "cached_input_rmb_per_million", "multiplier"})


def _canonical(body: dict) -> str:
    return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _check_string(value: object, name: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"invalid {name}")
    return value


def _check_hash(value: object, name: str) -> str:
    if type(value) is not str or not _HEX.fullmatch(value):
        raise ValueError(f"invalid {name}")
    return value


def _policy(raw: object) -> BillingPolicy:
    if type(raw) is not dict or set(raw) != {"welcome_points", "points_per_rmb", "pricing_version", "models"}:
        raise ValueError("invalid policy fields")
    rates = raw["models"]
    if type(rates) is not dict or not rates:
        raise ValueError("invalid policy rates")
    for name, rate in rates.items():
        _check_string(name, "policy model")
        if type(rate) is not dict or set(rate) != _RATE_FIELDS:
            raise ValueError("invalid policy rate fields")
        if any(type(value) is not str for value in rate.values()):
            raise ValueError("invalid policy rate type")
    result = BillingPolicy.from_dict(raw, {})
    if result is None or result.to_dict() != raw:
        raise ValueError("invalid policy")
    return result


@dataclass(frozen=True)
class TurnRecoveryContext:
    player_id: str
    game_id: str
    request_id: str
    turn_id: str
    input_hash: str
    generation_identity: str
    billing_enabled: bool
    base_version: int
    base_tick: int
    base_state_hash: str
    base_visible_observation_ids: tuple[str, ...]
    created_order: int
    canonical_json: str
    checksum: str

    def policy(self) -> BillingPolicy | None:
        raw = json.loads(self.canonical_json)["policy"]
        return _policy(raw) if raw is not None else None

    @classmethod
    def from_row(cls, row) -> TurnRecoveryContext:
        try:
            raw = row["canonical_json"]
            checksum = row["checksum"]
            if type(raw) is not str or _check_hash(checksum, "checksum") != sha256(raw.encode()).hexdigest():
                raise ValueError("context checksum mismatch")
            body = json.loads(raw)
            if type(body) is not dict or set(body) != _FIELDS or _canonical(body) != raw:
                raise ValueError("invalid context body")
            if type(body["format_version"]) is not int or body["format_version"] != 1:
                raise ValueError("unsupported context format")
            if body["scope"] != "ordinary_freeform_interactive":
                raise ValueError("invalid context scope")
            for name in ("player_id", "game_id", "request_id", "turn_id"):
                _check_string(body[name], name)
            if not _REQUEST.fullmatch(body["request_id"]) or body["turn_id"] != "portal-" + body["request_id"]:
                raise ValueError("invalid request identity")
            for name in ("input_hash", "generation_identity", "base_state_hash"):
                _check_hash(body[name], name)
            for name in ("base_version", "base_tick", "created_order"):
                if type(body[name]) is not int or body[name] < (1 if name == "created_order" else 0):
                    raise ValueError(f"invalid {name}")
            if type(body["billing_enabled"]) is not bool:
                raise ValueError("invalid billing mode")
            ids = body["base_visible_observation_ids"]
            if type(ids) is not list or any(type(value) is not str or not value for value in ids) or len(ids) != len(set(ids)):
                raise ValueError("invalid observation IDs")
            if body["billing_enabled"]:
                _policy(body["policy"])
            elif body["policy"] is not None:
                raise ValueError("free context cannot contain policy")
            for name in ("player_id", "game_id", "request_id", "turn_id", "input_hash",
                         "generation_identity", "created_order"):
                if row[name] != body[name]:
                    raise ValueError(f"indexed {name} mismatch")
            return cls(body["player_id"], body["game_id"], body["request_id"],
                       body["turn_id"], body["input_hash"], body["generation_identity"],
                       body["billing_enabled"], body["base_version"], body["base_tick"],
                       body["base_state_hash"], tuple(ids), body["created_order"], raw, checksum)
        except (ValueError, TypeError, KeyError, OverflowError) as error:
            raise TurnRecoveryContextError("turn recovery context is invalid") from error


class SQLTurnRecoveryContexts:
    def __init__(self, engine: Engine):
        self.engine = engine

    @staticmethod
    def _get(db, game_id: str, request_id: str) -> TurnRecoveryContext | None:
        row = db.execute(text("""SELECT * FROM turn_recovery_contexts
            WHERE game_id=:game_id AND request_id=:request_id"""),
            {"game_id": game_id, "request_id": request_id}).mappings().first()
        return TurnRecoveryContext.from_row(row) if row is not None else None

    def get(self, game_id: str, request_id: str) -> TurnRecoveryContext | None:
        with self.engine.connect() as db:
            return self._get(db, game_id, request_id)

    def is_latest(self, game_id: str, request_id: str) -> bool:
        with self.engine.connect() as db:
            context = self._get(db, game_id, request_id)
            if context is None:
                return False
            latest = db.execute(text("""SELECT * FROM turn_recovery_contexts
                WHERE game_id=:game_id ORDER BY created_order DESC LIMIT 1"""),
                {"game_id": game_id}).mappings().one()
            return TurnRecoveryContext.from_row(latest).request_id == request_id

    def freeze(self, *, player_id: str, game_id: str, request_id: str,
               input_hash: str, generation_identity: str, billing_enabled: bool,
               policy: BillingPolicy | None) -> tuple[TurnRecoveryContext, bool]:
        try:
            _check_string(player_id, "player ID")
            _check_string(game_id, "game ID")
            if type(request_id) is not str or not _REQUEST.fullmatch(request_id):
                raise ValueError("invalid request ID")
            _check_hash(input_hash, "input hash")
            _check_hash(generation_identity, "generation identity")
            if type(billing_enabled) is not bool:
                raise ValueError("invalid billing mode")
        except (ValueError, TypeError) as error:
            raise TurnRecoveryContextError("invalid recovery context request") from error

        with self.engine.begin() as db:
            if db.dialect.name == "sqlite":
                db.exec_driver_sql("BEGIN IMMEDIATE")
            lock = " FOR UPDATE" if db.dialect.name == "postgresql" else ""
            game = db.execute(text("SELECT * FROM games WHERE game_id=:game_id" + lock),
                              {"game_id": game_id}).mappings().first()
            owner = db.execute(text("""SELECT player_id FROM player_saves WHERE game_id=:game_id"""),
                               {"game_id": game_id}).scalar_one_or_none()
            if game is None or owner != player_id:
                raise TurnRecoveryContextError("save is unavailable to player")
            turn_id = "portal-" + request_id
            existing = self._get(db, game_id, request_id)
            if existing is not None and (existing.player_id != player_id or existing.input_hash != input_hash
                                         or existing.generation_identity != generation_identity):
                raise TurnRecoveryContextError("request recovery identity changed")
            scene = db.execute(text("""SELECT canonical_json FROM generated_scene_snapshots
                WHERE game_id=:game_id AND turn_id=:turn_id"""),
                {"game_id": game_id, "turn_id": turn_id}).first()
            committed = db.execute(text("""SELECT event_id FROM events WHERE game_id=:game_id
                AND substr(event_id,1,:prefix_length)=:prefix LIMIT 1"""),
                {"game_id": game_id, "prefix": turn_id + ":",
                 "prefix_length": len(turn_id) + 1}).first()
            receipt = db.execute(text("""SELECT request_id FROM portal_turns
                WHERE game_id=:game_id AND request_id=:request_id"""),
                {"game_id": game_id, "request_id": request_id}).first()
            if existing is None and (scene or committed or receipt):
                raise TurnRecoveryContextError("legacy request cannot acquire recovery context")
            snapshot = Snapshot(game_id, game["version"], game["tick"], json.loads(game["data"]))
            state_hash = snapshot_state_fingerprint(snapshot)
            rows = db.execute(text("""SELECT o.observation_id FROM observations AS o
                JOIN events AS e ON e.game_id=o.game_id AND e.event_id=o.event_id
                WHERE o.game_id=:game_id AND o.recipient_id=:recipient_id
                ORDER BY e.state_version,o.created_order,o.observation_id"""),
                {"game_id": game_id, "recipient_id": "player"}).scalars().all()
            if existing is not None:
                if scene is not None:
                    from storyloop_harness.advanced import GeneratedSceneSnapshot
                    row = db.execute(text("""SELECT snapshot_id,canonical_json,checksum
                        FROM generated_scene_snapshots WHERE game_id=:game_id AND turn_id=:turn_id"""),
                        {"game_id": game_id, "turn_id": turn_id}).mappings().one()
                    try:
                        saved = GeneratedSceneSnapshot.from_storage(row["canonical_json"], row["checksum"])
                    except (ValueError, TypeError) as error:
                        raise TurnRecoveryContextError("generated scene is invalid") from error
                    metadata = saved.metadata()
                    if (saved.game_id != game_id or saved.turn_id != turn_id or saved.snapshot_id != row["snapshot_id"]
                            or metadata["base_version"] != existing.base_version
                            or metadata["base_tick"] != existing.base_tick
                            or metadata["base_state_hash"] != existing.base_state_hash):
                        raise TurnRecoveryContextError("generated scene base differs from context")
                if existing.base_version != snapshot.version or existing.base_tick != snapshot.tick or existing.base_state_hash != state_hash:
                    if scene is None:
                        raise TurnRecoveryContextError("recovery base changed before scene generation")
                if scene is None and existing.base_visible_observation_ids != tuple(rows):
                    raise TurnRecoveryContextError("visible observations changed before scene generation")
                return existing, False
            try:
                if billing_enabled:
                    if not isinstance(policy, BillingPolicy):
                        raise ValueError("billed recovery requires policy")
                    policy_dict = policy.to_dict()
                    _policy(policy_dict)
                elif policy is not None:
                    raise ValueError("free recovery must omit policy")
                else:
                    policy_dict = None
            except (ValueError, TypeError) as error:
                raise TurnRecoveryContextError("invalid recovery billing terms") from error
            order = db.execute(text("""SELECT COALESCE(MAX(created_order),0)+1 FROM turn_recovery_contexts
                WHERE game_id=:game_id"""), {"game_id": game_id}).scalar_one()
            body = {"format_version": 1, "scope": "ordinary_freeform_interactive",
                    "player_id": player_id, "game_id": game_id, "request_id": request_id,
                    "turn_id": turn_id, "input_hash": input_hash,
                    "generation_identity": generation_identity, "billing_enabled": billing_enabled,
                    "policy": policy_dict, "base_version": snapshot.version, "base_tick": snapshot.tick,
                    "base_state_hash": state_hash, "base_visible_observation_ids": rows,
                    "created_order": order}
            canonical = _canonical(body)
            checksum = sha256(canonical.encode()).hexdigest()
            db.execute(text("""INSERT INTO turn_recovery_contexts
                (game_id,request_id,player_id,turn_id,input_hash,generation_identity,
                 created_order,canonical_json,checksum)
                VALUES (:game_id,:request_id,:player_id,:turn_id,:input_hash,:generation_identity,
                        :created_order,:canonical_json,:checksum)"""),
                {"game_id": game_id, "request_id": request_id, "player_id": player_id,
                 "turn_id": turn_id, "input_hash": input_hash, "generation_identity": generation_identity,
                 "created_order": order, "canonical_json": canonical, "checksum": checksum})
            result = self._get(db, game_id, request_id)
            if result is None:
                raise TurnRecoveryContextError("context was not persisted")
            return result, True
