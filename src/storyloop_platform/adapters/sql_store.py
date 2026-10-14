"""One game-state repository for SQLite and PostgreSQL."""

from __future__ import annotations

import json
import time
from dataclasses import asdict

from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from storyloop_harness.advanced import (
    AgentContextCheckpoint, AgentContextEntry, GeneratedSceneConflict,
    GeneratedSceneCorrupt, GeneratedSceneSnapshot, Observation, PendingWork,
    PlayerInput, Snapshot, WorldEvent, apply_event, validate_checkpoint,
    validate_event_snapshot_reference,
)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


class SQLGameStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def close(self) -> None:
        self.engine.dispose()

    @staticmethod
    def _snapshot(row) -> Snapshot:
        return Snapshot(row["game_id"], row["version"], row["tick"], json.loads(row["data"]))

    @staticmethod
    def _work(row) -> PendingWork:
        return PendingWork(row["work_id"], row["kind"], row["due_tick"], row["priority"],
                           row["cause_id"], json.loads(row["payload"]), bool(row["mandatory"]))

    @staticmethod
    def _insert_work(connection, game_id: str, work: tuple[PendingWork, ...]) -> None:
        for item in work:
            connection.execute(text("""INSERT INTO pending_work
                (game_id,work_id,kind,due_tick,priority,cause_id,payload,mandatory,status)
                VALUES (:game_id,:work_id,:kind,:due_tick,:priority,:cause_id,:payload,:mandatory,'pending')"""),
                {"game_id": game_id, "work_id": item.work_id, "kind": item.kind,
                 "due_tick": item.due_tick, "priority": item.priority, "cause_id": item.cause_id,
                 "payload": _json(item.payload), "mandatory": int(item.mandatory)})

    def create_game(self, snapshot: Snapshot,
                    initial_work: tuple[PendingWork, ...] = ()) -> None:
        try:
            with self.engine.begin() as db:
                db.execute(text("""INSERT INTO games (game_id,version,tick,data)
                    VALUES (:game_id,:version,:tick,:data)"""),
                    {"game_id": snapshot.game_id, "version": snapshot.version,
                     "tick": snapshot.tick, "data": _json(snapshot.data)})
                self._insert_work(db, snapshot.game_id, initial_work)
        except IntegrityError as error:
            raise ValueError(str(error)) from error

    def load(self, game_id: str) -> Snapshot:
        with self.engine.connect() as db:
            row = db.execute(text("SELECT * FROM games WHERE game_id=:game_id"),
                             {"game_id": game_id}).mappings().first()
        if row is None:
            raise KeyError(game_id)
        return self._snapshot(row)

    def event_exists(self, game_id: str, event_id: str) -> bool:
        with self.engine.connect() as db:
            row = db.execute(text("""SELECT 1 FROM events
                WHERE game_id=:game_id AND event_id=:event_id"""),
                {"game_id": game_id, "event_id": event_id}).first()
        return row is not None

    @staticmethod
    def _generated_scene_snapshot(db, game_id: str, turn_id: str) -> GeneratedSceneSnapshot | None:
        row = db.execute(text("""SELECT snapshot_id,canonical_json,checksum
            FROM generated_scene_snapshots WHERE game_id=:game_id AND turn_id=:turn_id"""),
            {"game_id": game_id, "turn_id": turn_id}).mappings().first()
        if row is None:
            return None
        record = GeneratedSceneSnapshot.from_storage(row["canonical_json"], row["checksum"])
        if (record.snapshot_id, record.game_id, record.turn_id) != (
                row["snapshot_id"], game_id, turn_id):
            raise GeneratedSceneCorrupt("generated scene row identity mismatch")
        return record

    def generated_scene_snapshot(self, game_id: str, turn_id: str) -> GeneratedSceneSnapshot | None:
        with self.engine.connect() as db:
            return self._generated_scene_snapshot(db, game_id, turn_id)

    def save_generated_scene_snapshot(self, snapshot: GeneratedSceneSnapshot) -> GeneratedSceneSnapshot:
        record = GeneratedSceneSnapshot.from_storage(snapshot.canonical_json, snapshot.checksum)
        if record != snapshot:
            raise GeneratedSceneCorrupt("generated scene wrapper mismatch")
        with self.engine.begin() as db:
            if db.dialect.name == "sqlite":
                db.exec_driver_sql("BEGIN IMMEDIATE")
            lock = " FOR UPDATE" if db.dialect.name == "postgresql" else ""
            game = db.execute(text("SELECT game_id FROM games WHERE game_id=:game_id" + lock),
                              {"game_id": record.game_id}).first()
            if game is None:
                raise KeyError(record.game_id)
            prior = self._generated_scene_snapshot(db, record.game_id, record.turn_id)
            if prior is not None:
                if prior != record:
                    raise GeneratedSceneConflict("generated scene already exists")
                return prior
            db.execute(text("""INSERT INTO generated_scene_snapshots
                (game_id,turn_id,snapshot_id,canonical_json,checksum)
                VALUES (:game_id,:turn_id,:snapshot_id,:canonical_json,:checksum)"""),
                {"game_id": record.game_id, "turn_id": record.turn_id,
                 "snapshot_id": record.snapshot_id, "canonical_json": record.canonical_json,
                 "checksum": record.checksum})
            return self._generated_scene_snapshot(db, record.game_id, record.turn_id)

    def event_details(self, game_id: str, event_id: str) -> dict[str, object] | None:
        with self.engine.connect() as db:
            row = db.execute(text("""SELECT details FROM events
                WHERE game_id=:game_id AND event_id=:event_id"""),
                {"game_id": game_id, "event_id": event_id}).mappings().first()
        return json.loads(row["details"]) if row else None

    def completed_turn_count(self, game_id: str) -> int:
        with self.engine.connect() as db:
            count = db.execute(text("""SELECT COUNT(*) FROM events
                WHERE game_id=:game_id AND kind='campaign_turn_completed'"""),
                {"game_id": game_id}).scalar_one()
        return int(count)

    def commit(self, game_id: str, expected_version: int, event: WorldEvent | None,
               observations: tuple[Observation, ...], new_work: tuple[PendingWork, ...],
               consumed_work_id: str | None = None) -> Snapshot:
        try:
            with self.engine.begin() as db:
                if db.dialect.name == "sqlite":
                    db.exec_driver_sql("BEGIN IMMEDIATE")
                lock = " FOR UPDATE" if db.dialect.name == "postgresql" else ""
                row = db.execute(text("SELECT * FROM games WHERE game_id=:game_id" + lock),
                                 {"game_id": game_id}).mappings().first()
                if row is None:
                    raise KeyError(game_id)
                if event is not None and any(key in event.details for key in (
                        "generated_scene_snapshot_id", "generated_scene_snapshot_checksum")):
                    referenced = self._generated_scene_snapshot(
                        db, game_id, event.event_id.rsplit(":", 1)[0])
                    validate_event_snapshot_reference(event, referenced)
                before = self._snapshot(row)
                if before.version != expected_version:
                    raise ValueError("game state version changed")
                if consumed_work_id is not None:
                    result = db.execute(text("""UPDATE pending_work SET status='consumed'
                        WHERE game_id=:game_id AND work_id=:work_id AND status='pending'"""),
                        {"game_id": game_id, "work_id": consumed_work_id})
                    if result.rowcount != 1:
                        raise ValueError("pending work was already consumed or does not exist")
                after = apply_event(before, event) if event is not None else before
                if event is not None:
                    updated = db.execute(text("""UPDATE games SET version=:version,tick=:tick,data=:data
                        WHERE game_id=:game_id AND version=:expected_version"""),
                        {"version": after.version, "tick": after.tick, "data": _json(after.data),
                         "game_id": game_id, "expected_version": expected_version})
                    if updated.rowcount != 1:
                        raise ValueError("game state version changed")
                    db.execute(text("""INSERT INTO events
                        (game_id,event_id,kind,actor_id,cause_id,tick,effects,details,state_version)
                        VALUES (:game_id,:event_id,:kind,:actor_id,:cause_id,:tick,:effects,:details,:state_version)"""),
                        {"game_id": game_id, "event_id": event.event_id, "kind": event.kind,
                         "actor_id": event.actor_id, "cause_id": event.cause_id, "tick": event.tick,
                         "effects": _json([asdict(effect) for effect in event.effects]),
                         "details": _json(event.details), "state_version": after.version})
                order = time.time_ns()
                for index, observation in enumerate(observations):
                    source = db.execute(text("""SELECT state_version FROM events
                        WHERE game_id=:game_id AND event_id=:event_id"""),
                        {"game_id": game_id, "event_id": observation.event_id}).mappings().first()
                    if source is None:
                        raise ValueError("observation refers to an unknown event")
                    cursor = db.execute(text("""SELECT through_version FROM agent_contexts
                        WHERE game_id=:game_id AND actor_id=:actor_id"""),
                        {"game_id": game_id, "actor_id": observation.recipient_id}).scalar_one_or_none()
                    if cursor is not None and source["state_version"] <= cursor:
                        raise ValueError("observation refers to a checkpointed event")
                    db.execute(text("""INSERT INTO observations
                        (game_id,observation_id,event_id,recipient_id,channel,content,tick,created_order)
                        VALUES (:game_id,:observation_id,:event_id,:recipient_id,:channel,:content,:tick,:created_order)"""),
                        {"game_id": game_id, "observation_id": observation.observation_id,
                         "event_id": observation.event_id, "recipient_id": observation.recipient_id,
                         "channel": observation.channel, "content": observation.content,
                         "tick": observation.tick, "created_order": order + index})
                self._insert_work(db, game_id, new_work)
                return after
        except IntegrityError as error:
            raise ValueError(str(error)) from error

    def observations_for(self, game_id: str, recipient_id: str,
                         *, limit: int | None = None) -> list[Observation]:
        if limit is not None and (type(limit) is not int or limit < 1):
            raise ValueError("history limit must be a positive integer")
        order = ("e.state_version,o.created_order,o.observation_id" if limit is None else
                 "e.state_version DESC,o.created_order DESC,o.observation_id DESC LIMIT :limit")
        with self.engine.connect() as db:
            rows = db.execute(text("""SELECT o.observation_id,o.event_id,o.recipient_id,
                o.channel,o.content,o.tick FROM observations AS o
                JOIN events AS e ON e.game_id=o.game_id AND e.event_id=o.event_id
                WHERE o.game_id=:game_id AND o.recipient_id=:recipient_id
                ORDER BY """ + order),
                {"game_id": game_id, "recipient_id": recipient_id, "limit": limit}).mappings().all()
        if limit is not None:
            rows.reverse()
        return [Observation(**dict(row)) for row in rows]

    def dialogue_history_for_actor(self, game_id: str, actor_id: str) -> list[tuple[str, str]]:
        with self.engine.connect() as db:
            rows = db.execute(text("""SELECT details FROM events
                WHERE game_id=:game_id AND actor_id=:actor_id AND kind='npc_spoke'
                ORDER BY state_version"""),
                {"game_id": game_id, "actor_id": actor_id}).mappings().all()
        return [(value["player_message"], value["speech"])
                for value in (json.loads(row["details"]) for row in rows)]

    def player_inputs_for(self, game_id: str, *, limit: int | None = None) -> list[PlayerInput]:
        if limit is not None and (type(limit) is not int or limit < 1):
            raise ValueError("history limit must be a positive integer")
        order = "state_version" if limit is None else "state_version DESC LIMIT :limit"
        with self.engine.connect() as db:
            rows = db.execute(text("""SELECT event_id,kind,tick,details FROM events
                WHERE game_id=:game_id AND kind IN
                ('player_input','player_query','player_action','action_rejected')
                AND actor_id='player' ORDER BY """ + order),
                {"game_id": game_id, "limit": limit}).mappings().all()
        if limit is not None:
            rows.reverse()
        result: list[PlayerInput] = []
        for row in rows:
            details = json.loads(row["details"])
            channel = {"player_query": "query", "player_action": "action",
                       "action_rejected": "action"}.get(row["kind"], "speech")
            result.append(PlayerInput(row["event_id"], row["tick"], details["text"],
                                      details.get("channel", channel),
                                      tuple(details.get("target_ids", []))))
        return result

    def agent_context_entries(self, game_id: str, actor_id: str,
                              after_version: int = -1) -> list[AgentContextEntry]:
        """Read committed input/dialogue and only this actor's observations."""
        with self.engine.connect() as db:
            return self._context_entries(db, game_id, actor_id, after_version)

    @staticmethod
    def _context_entries(db, game_id: str, actor_id: str,
                         after_version: int) -> list[AgentContextEntry]:
        rows = db.execute(text("""SELECT 0 AS record_type,e.event_id AS entry_id,
                e.event_id,e.kind,e.tick,e.state_version,e.cause_id,e.details,
                NULL AS content,0 AS created_order
                FROM events AS e WHERE e.game_id=:game_id
                AND e.state_version>:after_version
                AND ((:actor_id='player' AND e.actor_id='player'
                    AND e.kind IN ('player_input','player_query','player_action','action_rejected'))
                    OR (:actor_id<>'player' AND e.actor_id=:actor_id AND e.kind='npc_spoke'))
                UNION ALL
                SELECT 1 AS record_type,o.observation_id AS entry_id,
                o.event_id,o.channel AS kind,o.tick,e.state_version,
                NULL AS cause_id,NULL AS details,o.content,o.created_order
                FROM observations AS o JOIN events AS e
                ON e.game_id=o.game_id AND e.event_id=o.event_id
                WHERE o.game_id=:game_id AND o.recipient_id=:actor_id
                AND e.state_version>:after_version
                ORDER BY state_version,record_type,created_order,entry_id"""),
                {"game_id": game_id, "actor_id": actor_id,
                 "after_version": after_version}).mappings().all()
        observed_messages = {(row["event_id"], row["content"])
                             for row in rows if row["record_type"] == 1}
        entries: list[AgentContextEntry] = []
        for row in rows:
            if row["record_type"] == 0:
                details = json.loads(row["details"])
                if row["kind"] == "npc_spoke":
                    message = str(details.get("player_message", ""))
                    prefix = ("" if (row["cause_id"], message) in observed_messages
                              else f"玩家曾说：{message}\n")
                    content = f"{prefix}你当时回答：{details.get('speech', '')}"
                else:
                    content = str(details.get("text", ""))
            else:
                content = row["content"]
            entries.append(AgentContextEntry(row["state_version"], row["entry_id"],
                                             row["kind"], content, row["tick"]))
        return entries

    def agent_context_checkpoint(self, game_id: str, actor_id: str) -> AgentContextCheckpoint:
        with self.engine.connect() as db:
            row = db.execute(text("""SELECT through_version,summary,scenario_id,scenario_version,source_ids FROM agent_contexts
                WHERE game_id=:game_id AND actor_id=:actor_id"""),
                {"game_id": game_id, "actor_id": actor_id}).mappings().first()
        return (AgentContextCheckpoint(row["through_version"], row["summary"],
                                       row["scenario_id"], row["scenario_version"],
                                       tuple(json.loads(row["source_ids"])))
                if row is not None else AgentContextCheckpoint())

    def save_agent_context_checkpoint(self, game_id: str, actor_id: str,
                                      expected_version: int,
                                      checkpoint: AgentContextCheckpoint) -> None:
        with self.engine.begin() as db:
            if db.dialect.name == "sqlite":
                db.exec_driver_sql("BEGIN IMMEDIATE")
            lock = " FOR UPDATE" if db.dialect.name == "postgresql" else ""
            game = db.execute(text("SELECT * FROM games WHERE game_id=:game_id" + lock),
                              {"game_id": game_id}).mappings().first()
            if game is None:
                raise KeyError(game_id)
            current = db.execute(text("""SELECT through_version FROM agent_contexts
                WHERE game_id=:game_id AND actor_id=:actor_id"""),
                {"game_id": game_id, "actor_id": actor_id}).scalar_one_or_none()
            if (current if current is not None else -1) != expected_version:
                raise ValueError("agent context checkpoint changed")
            validate_checkpoint(self._snapshot(game),
                                self._context_entries(db, game_id, actor_id, expected_version),
                                expected_version, checkpoint)
            params = {"game_id": game_id, "actor_id": actor_id, "expected": expected_version,
                      "through_version": checkpoint.through_version, "summary": checkpoint.summary,
                      "scenario_id": checkpoint.scenario_id,
                      "scenario_version": checkpoint.scenario_version,
                      "source_ids": _json(checkpoint.source_ids)}
            updated = db.execute(text("""UPDATE agent_contexts
                SET through_version=:through_version,summary=:summary,
                    scenario_id=:scenario_id,scenario_version=:scenario_version,source_ids=:source_ids
                WHERE game_id=:game_id AND actor_id=:actor_id AND through_version=:expected"""),
                params)
            if updated.rowcount == 1:
                return
            if expected_version != -1:
                raise ValueError("agent context checkpoint changed")
            try:
                db.execute(text("""INSERT INTO agent_contexts
                    (game_id,actor_id,through_version,summary,scenario_id,scenario_version,source_ids)
                    VALUES (:game_id,:actor_id,:through_version,:summary,:scenario_id,:scenario_version,:source_ids)"""),
                    params)
            except IntegrityError as error:
                raise ValueError("agent context checkpoint changed") from error

    def ready_work(self, game_id: str, tick: int) -> list[PendingWork]:
        with self.engine.connect() as db:
            rows = db.execute(text("""SELECT * FROM pending_work WHERE game_id=:game_id
                AND due_tick<=:tick AND status='pending'
                ORDER BY due_tick,mandatory DESC,priority DESC,work_id"""),
                {"game_id": game_id, "tick": tick}).mappings().all()
        return [self._work(row) for row in rows]

    def pending_work(self, game_id: str) -> list[PendingWork]:
        with self.engine.connect() as db:
            rows = db.execute(text("""SELECT * FROM pending_work WHERE game_id=:game_id
                AND status='pending' ORDER BY due_tick,mandatory DESC,priority DESC,work_id"""),
                {"game_id": game_id}).mappings().all()
        return [self._work(row) for row in rows]
