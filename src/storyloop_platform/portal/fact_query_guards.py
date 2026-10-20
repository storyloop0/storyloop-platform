"""Read-only guards for the ordinary request namespace shared by fact queries."""

import json

from sqlalchemy import Engine, text

from storyloop_platform.portal.turn_errors import TurnRecoveryRequired


_EVIDENCE = (
    ("turn_recovery_contexts", "request_id"),
    ("turn_settlement_intents", "request_id"),
    ("generated_scene_snapshots", "turn_id"),
    ("generated_scene_generation_markers", "turn_id"),
)


def require_no_ordinary_evidence(engine: Engine, game_id: str, request_id: str,
                                 *, committed_fact: bool, same_id_only: bool = False) -> None:
    """A fact cannot take an ordinary ID or advance past unresolved ordinary work."""
    with engine.connect() as db:
        completed_turns = set(db.execute(text("""SELECT request_id FROM portal_turns
            WHERE game_id=:game_id AND request_kind='turn'"""),
            {"game_id": game_id}).scalars())
        for table, field in _EVIDENCE:
            values = db.execute(text(f"SELECT {field} FROM {table} WHERE game_id=:game_id"),
                                {"game_id": game_id}).scalars()
            for value in values:
                ordinary_id = value[7:] if field == "turn_id" and value.startswith("portal-") else value
                if ordinary_id == request_id:
                    raise TurnRecoveryRequired("请求 ID 已属于普通行动恢复记录。")
                if not same_id_only and not committed_fact and ordinary_id not in completed_turns:
                    raise TurnRecoveryRequired("存档有未完成的普通行动，请先恢复该行动。")
        rows = db.execute(text("""SELECT event_id,details FROM events
            WHERE game_id=:game_id AND event_id LIKE 'portal-%'"""),
            {"game_id": game_id}).mappings().all()
        fact_ids = set()
        for row in rows:
            turn_id, separator, suffix = row["event_id"].rpartition(":")
            if not separator or not turn_id.startswith("portal-") or suffix != "input":
                continue
            try:
                details = json.loads(row["details"])
            except (TypeError, ValueError):
                continue
            if isinstance(details, dict) and isinstance(details.get("fact_query"), dict):
                fact_ids.add(turn_id[7:])
        for row in rows:
            turn_id, separator, suffix = row["event_id"].rpartition(":")
            if (not separator or not turn_id.startswith("portal-")
                    or suffix not in {"input", "query", "action", "continue", "choice", "advance", "completed"}):
                continue
            ordinary_id = turn_id[7:]
            if ordinary_id in fact_ids:
                continue
            if ordinary_id == request_id:
                raise TurnRecoveryRequired("请求 ID 已属于普通行动。")
            if not same_id_only and not committed_fact and ordinary_id not in completed_turns:
                raise TurnRecoveryRequired("存档有未完成的普通行动，请先恢复该行动。")
