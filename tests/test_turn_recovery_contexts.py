"""Frozen pre-call evidence for ordinary player scene recovery."""

from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
from threading import Barrier

import pytest
from sqlalchemy import text

from storyloop_harness.advanced import (
    GeneratedSceneSnapshot, Observation, SceneTurn, Snapshot, WorldEvent,
)
from storyloop_platform.adapters.store import SQLiteGameStore
from storyloop_platform.portal.billing import BillingPolicy
from storyloop_platform.portal.sql_repository import SQLPlayerRepository
from storyloop_platform.portal.turn_recovery_contexts import (
    SQLTurnRecoveryContexts, TurnRecoveryContextError,
)


HASH = sha256(b"Look at the harbor").hexdigest()
IDENTITY = "a" * 64


@pytest.fixture
def setup(tmp_path):
    path = str(tmp_path / "save.db")
    store = SQLiteGameStore(path)
    player = SQLPlayerRepository(store.engine).register("reader", "password123")
    store.create_game(Snapshot("game", 0, 0, {"world": {"flag": False}}))
    SQLPlayerRepository(store.engine).create_save(player, "catalog", "game", "pkg", "1", "b" * 64)
    yield store, player, path
    store.close()


def policy(price="1"):
    return BillingPolicy.from_dict({"welcome_points": 0, "points_per_rmb": 100,
        "pricing_version": "v1", "models": {"model": {
            "input_rmb_per_million": price, "output_rmb_per_million": price,
            "cached_input_rmb_per_million": "0", "multiplier": "1"}}}, {})


def freeze(contexts, player, **changes):
    args = dict(player_id=player, game_id="game", request_id="req1", input_hash=HASH,
                generation_identity=IDENTITY, billing_enabled=True, policy=policy())
    return contexts.freeze(**(args | changes))


def test_freeze_reopen_first_terms_and_detached_policy(setup):
    store, player, path = setup
    contexts = SQLTurnRecoveryContexts(store.engine)
    first, created = freeze(contexts, player)
    assert created is True
    assert (first.turn_id, first.base_version, first.base_tick) == ("portal-req1", 0, 0)
    assert first.base_visible_observation_ids == ()
    assert first.policy().rates["model"].input_rmb_per_million == 1
    first.policy().rates.clear()
    second, created = freeze(contexts, player, policy=policy("9"), billing_enabled=False)
    assert created is False
    assert second == first
    reopened = SQLiteGameStore(path)
    try:
        assert SQLTurnRecoveryContexts(reopened.engine).get("game", "req1") == first
    finally:
        reopened.close()
    assert store.load("game").version == 0


@pytest.mark.parametrize("change", [
    {"input_hash": "c" * 64}, {"generation_identity": "d" * 64},
    {"player_id": "other"},
])
def test_retry_rejects_changed_identity(setup, change):
    store, player, _ = setup
    contexts = SQLTurnRecoveryContexts(store.engine)
    freeze(contexts, player)
    with pytest.raises(TurnRecoveryContextError):
        freeze(contexts, player, **change)


def test_concurrent_freeze_has_one_creator_and_latest_pending_order(setup):
    store, player, path = setup
    barrier = Barrier(2)
    stores = (SQLiteGameStore(path), SQLiteGameStore(path))
    def run(item):
        try:
            barrier.wait(timeout=10)
            return freeze(SQLTurnRecoveryContexts(item.engine), player)[1]
        finally:
            item.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(run, stores)) == [False, True]
    contexts = SQLTurnRecoveryContexts(store.engine)
    second, created = freeze(contexts, player, request_id="req2")
    assert created and second.created_order > contexts.get("game", "req1").created_order
    assert not contexts.is_latest("game", "req1")
    assert contexts.is_latest("game", "req2")


def test_legacy_scene_or_any_committed_suffix_cannot_be_adopted(setup):
    store, player, _ = setup
    contexts = SQLTurnRecoveryContexts(store.engine)
    with store.engine.begin() as db:
        db.execute(text("""INSERT INTO generated_scene_snapshots
            (game_id,turn_id,snapshot_id,canonical_json,checksum)
            VALUES ('game','portal-req1','x','{}','x')"""))
    with pytest.raises(TurnRecoveryContextError):
        freeze(contexts, player)
    with store.engine.begin() as db:
        db.execute(text("DELETE FROM generated_scene_snapshots"))
        db.execute(text("""INSERT INTO events
            (game_id,event_id,kind,actor_id,tick,effects,details,state_version)
            VALUES ('game','portal-req1:npc','npc_spoke','npc',1,'[]','{}',1)"""))
    with pytest.raises(TurnRecoveryContextError):
        freeze(contexts, player)
    assert contexts.get("game", "req1") is None


def test_repeat_requires_same_base_when_scene_missing(setup):
    store, player, _ = setup
    contexts = SQLTurnRecoveryContexts(store.engine)
    freeze(contexts, player)
    with store.engine.begin() as db:
        db.execute(text("UPDATE games SET version=1,data='{}' WHERE game_id='game'"))
    with pytest.raises(TurnRecoveryContextError):
        freeze(contexts, player)


def test_repeat_rejects_changed_visible_observations_without_scene(setup):
    store, player, _ = setup
    contexts = SQLTurnRecoveryContexts(store.engine)
    freeze(contexts, player)
    with store.engine.begin() as db:
        db.execute(text("""INSERT INTO events
            (game_id,event_id,kind,actor_id,tick,effects,details,state_version)
            VALUES ('game','event','marker','player',0,'[]','{}',0)"""))
        db.execute(text("""INSERT INTO observations
            (game_id,observation_id,event_id,recipient_id,channel,content,tick,created_order)
            VALUES ('game','new','event','player','scene','seen',0,1)"""))
    with pytest.raises(TurnRecoveryContextError):
        freeze(contexts, player)


def test_free_mode_keeps_null_policy_after_retry(setup):
    store, player, _ = setup
    contexts = SQLTurnRecoveryContexts(store.engine)
    first, created = freeze(contexts, player, billing_enabled=False, policy=None)
    assert created and first.policy() is None
    again, created = freeze(contexts, player, billing_enabled=True, policy=policy())
    assert not created and again == first and again.policy() is None


def test_failed_insert_leaves_no_context_or_world_change(setup):
    store, player, _ = setup
    contexts = SQLTurnRecoveryContexts(store.engine)
    before = store.load("game")
    with store.engine.begin() as db:
        db.execute(text("""CREATE TRIGGER reject_context BEFORE INSERT ON turn_recovery_contexts
            BEGIN SELECT RAISE(ABORT, 'blocked'); END"""))
    with pytest.raises(Exception):
        freeze(contexts, player)
    assert contexts.get("game", "req1") is None
    assert store.load("game") == before


def test_snapshot_base_mismatch_rejects_repeat(setup):
    store, player, _ = setup
    contexts = SQLTurnRecoveryContexts(store.engine)
    freeze(contexts, player)
    scene = GeneratedSceneSnapshot.create(
        game_id="game", turn_id="portal-req1", input_hash=HASH,
        base_version=0, base_tick=0, base_state_hash="0" * 64,
        scenario_id="harbor", scenario_version="1", package_fingerprint="b" * 64,
        system_prompt_hash="c" * 64, output_schema_hash="d" * 64,
        commit_config_hash="e" * 64, generation_config_hash="f" * 64,
        projected_request_hash="1" * 64, focus_actor_ids=(), nearby_actor_ids=(),
        preference_hash="2" * 64,
        scene=SceneTurn.model_validate({
            "decision": {"intent": "speech", "target_ids": []},
            "prose": "The harbor is quiet.", "replies": [],
            "options": [{"label": "Look", "input": "Look around"},
                        {"label": "Wait", "input": "Wait here"},
                        {"label": "Leave", "input": "Leave the harbor"}],
        }), success_usage=(), usage_coverage="unobserved")
    with store.engine.begin() as db:
        db.execute(text("""INSERT INTO generated_scene_snapshots
            (game_id,turn_id,snapshot_id,canonical_json,checksum)
            VALUES ('game','portal-req1',:snapshot_id,:canonical_json,:checksum)"""),
            {"snapshot_id": scene.snapshot_id, "canonical_json": scene.canonical_json,
             "checksum": scene.checksum})
    with pytest.raises(TurnRecoveryContextError):
        freeze(contexts, player)


def test_context_cascades_on_game_delete(setup):
    store, player, _ = setup
    contexts = SQLTurnRecoveryContexts(store.engine)
    freeze(contexts, player)
    with store.engine.begin() as db:
        db.exec_driver_sql("PRAGMA foreign_keys=ON")
        db.execute(text("DELETE FROM games WHERE game_id='game'"))
    assert contexts.get("game", "req1") is None


def test_observation_ids_follow_store_order_and_reject_duplicates(setup):
    store, player, _ = setup
    store.commit("game", 0, WorldEvent("event", "marker", "player", None, 1, ()),
                 (Observation("obs1", "event", "player", "scene", "first", 1),
                  Observation("obs2", "event", "player", "scene", "second", 1)), ())
    contexts = SQLTurnRecoveryContexts(store.engine)
    value, _ = freeze(contexts, player)
    assert value.base_visible_observation_ids == ("obs1", "obs2")
    assert value.base_version == 1


def test_freeze_captures_only_literal_player_actor_observations(setup):
    store, player, _ = setup
    with store.engine.begin() as db:
        for event_id, actor_id, observation_id, recipient_id, created_order in (
            ("player-event", "player", "player-observation", "player", 1),
            ("npc-event", "dockhand", "npc-observation", "dockhand", 2),
            ("account-event", "player", "account-observation", player, 3),
        ):
            db.execute(text("""INSERT INTO events
                (game_id,event_id,kind,actor_id,tick,effects,details,state_version)
                VALUES ('game',:event_id,'marker',:actor_id,0,'[]','{}',0)"""),
                {"event_id": event_id, "actor_id": actor_id})
            db.execute(text("""INSERT INTO observations
                (game_id,observation_id,event_id,recipient_id,channel,content,tick,created_order)
                VALUES ('game',:observation_id,:event_id,:recipient_id,'scene','seen',0,:created_order)"""),
                {"observation_id": observation_id, "event_id": event_id,
                 "recipient_id": recipient_id, "created_order": created_order})
    contexts = SQLTurnRecoveryContexts(store.engine)
    value, created = freeze(contexts, player)
    assert created
    assert value.player_id == player
    assert value.base_visible_observation_ids == ("player-observation",)
    assert store.load("game").version == 0


@pytest.mark.parametrize("column,value", [
    ("checksum", "0" * 64), ("canonical_json", "{}"),
    ("input_hash", "0" * 64), ("created_order", 99),
])
def test_tampered_row_fails_closed(setup, column, value):
    store, player, _ = setup
    contexts = SQLTurnRecoveryContexts(store.engine)
    freeze(contexts, player)
    with store.engine.begin() as db:
        db.execute(text(f"UPDATE turn_recovery_contexts SET {column}=:value"), {"value": value})
    with pytest.raises(TurnRecoveryContextError):
        contexts.get("game", "req1")


def test_latest_fails_closed_when_newest_row_is_tampered(setup):
    store, player, _ = setup
    contexts = SQLTurnRecoveryContexts(store.engine)
    freeze(contexts, player)
    freeze(contexts, player, request_id="req2")
    with store.engine.begin() as db:
        db.execute(text("""UPDATE turn_recovery_contexts SET checksum=:checksum
            WHERE game_id='game' AND request_id='req2'"""), {"checksum": "0" * 64})
    with pytest.raises(TurnRecoveryContextError):
        contexts.is_latest("game", "req1")


@pytest.mark.parametrize("change", [
    {"format_version": 2}, {"extra": 1}, {"base_tick": True},
    {"base_visible_observation_ids": ["dup", "dup"]},
    {"policy": {"welcome_points": True}},
])
def test_invalid_canonical_body_even_with_recomputed_checksum(setup, change):
    store, player, _ = setup
    contexts = SQLTurnRecoveryContexts(store.engine)
    first, _ = freeze(contexts, player)
    body = json.loads(first.canonical_json)
    body.update(change)
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with store.engine.begin() as db:
        db.execute(text("UPDATE turn_recovery_contexts SET canonical_json=:body,checksum=:checksum"),
                   {"body": encoded, "checksum": sha256(encoded.encode()).hexdigest()})
    with pytest.raises(TurnRecoveryContextError):
        contexts.get("game", "req1")
