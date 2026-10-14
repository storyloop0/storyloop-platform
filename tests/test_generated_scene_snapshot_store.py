"""Platform's SQL store adopts the Harness generated scene contract."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from hashlib import sha256
from threading import Barrier

import pytest
from sqlalchemy import text

from storyloop_harness.advanced import (
    GeneratedSceneConflict, GeneratedSceneCorrupt, GeneratedSceneSnapshot,
    Observation, PendingWork, SceneTurn, Snapshot, WorldEvent,
)
from storyloop_platform.adapters.store import SQLiteGameStore


def record(game_id="game", **changes):
    fields = dict(game_id=game_id, turn_id="turn", input_hash=sha256(b"Hello").hexdigest(),
                  base_version=0, base_tick=0, base_state_hash="b" * 64,
                  scenario_id="harbor", scenario_version="1", package_fingerprint="c" * 64,
                  system_prompt_hash="d" * 64, output_schema_hash="e" * 64,
                  commit_config_hash="f" * 64, generation_config_hash="1" * 64,
                  projected_request_hash="2" * 64, focus_actor_ids=("dockhand",),
                  nearby_actor_ids=("dockhand",), preference_hash="3" * 64,
                  scene=SceneTurn.model_validate({
                      "decision": {"intent": "speech", "target_ids": ["dockhand"]},
                      "prose": "The dockhand sets down a rope.",
                      "replies": [{"actor_id": "dockhand", "speech": "A ship arrived."}],
                      "options": [{"label": "Ask", "input": "Ask about ships"},
                                  {"label": "Look", "input": "Read the notice"},
                                  {"label": "Walk", "input": "Walk along the pier"}],
                  }), success_usage=(), usage_coverage="unobserved")
    return GeneratedSceneSnapshot.create(**{**fields, **changes})


@pytest.fixture
def store(tmp_path):
    item = SQLiteGameStore(str(tmp_path / "game.db"))
    item.create_game(Snapshot("game", 0, 0, {"world": {"flag": False}}))
    yield item
    item.close()


def test_insert_reopen_idempotence_and_no_world_mutation(store):
    before = store.load("game")
    saved = record()
    assert store.save_generated_scene_snapshot(saved) == saved
    assert store.save_generated_scene_snapshot(saved) == saved
    reopened = SQLiteGameStore(store.path)
    try:
        assert reopened.generated_scene_snapshot("game", "turn") == saved
        assert reopened.generated_scene_snapshot("game", "missing") is None
    finally:
        reopened.close()
    assert store.load("game") == before
    assert store.player_inputs_for("game") == []
    assert store.pending_work("game") == []


def test_conflict_foreign_key_and_failed_save_preserve_first_record(store):
    first = record()
    store.save_generated_scene_snapshot(first)
    with pytest.raises(GeneratedSceneConflict):
        store.save_generated_scene_snapshot(record(input_hash="9" * 64))
    with pytest.raises(KeyError):
        store.save_generated_scene_snapshot(record(game_id="other"))
    with pytest.raises(GeneratedSceneCorrupt):
        store.save_generated_scene_snapshot(replace(first, checksum="0" * 64))
    assert store.generated_scene_snapshot("game", "turn") == first
    assert store.load("game").version == 0


def test_reference_validation_rolls_back_all_commit_writes(store):
    saved = record()
    details = dict(text="Hello", single_call=saved.scene().model_dump(),
                   generated_scene_snapshot_id=saved.snapshot_id,
                   generated_scene_snapshot_checksum=saved.checksum)
    observation = Observation("obs", "turn:input", "player", "scene", "seen", 1)
    work = PendingWork("next", "next", 1, 1, "turn:input", {})

    def attempt(payload):
        event = WorldEvent("turn:input", "player_input", "player", None, 1, (), payload)
        with pytest.raises(GeneratedSceneCorrupt):
            store.commit("game", 0, event, (observation,), (work,))
        assert store.load("game").version == 0
        assert not store.event_exists("game", "turn:input")
        assert store.observations_for("game", "player") == []
        assert store.pending_work("game") == []

    attempt(details)
    store.save_generated_scene_snapshot(saved)
    attempt({**details, "generated_scene_snapshot_checksum": "0" * 64})
    attempt({**details, "text": "Wrong"})
    assert store.commit("game", 0, WorldEvent("turn:input", "player_input", "player",
                        None, 1, (), details), (observation,), (work,)).version == 1


def test_corrupt_row_fails_closed(store):
    store.save_generated_scene_snapshot(record())
    with store.engine.begin() as db:
        db.execute(text("UPDATE generated_scene_snapshots SET checksum=:checksum"),
                   {"checksum": "0" * 64})
    with pytest.raises(GeneratedSceneCorrupt):
        store.generated_scene_snapshot("game", "turn")


def test_two_store_instances_accept_one_winner(tmp_path):
    path = str(tmp_path / "race.db")
    seed = SQLiteGameStore(path)
    seed.create_game(Snapshot("game", 0, 0, {}))
    seed.close()
    candidates = (record(), record(input_hash="9" * 64))
    barrier = Barrier(2)

    stores = (SQLiteGameStore(path), SQLiteGameStore(path))

    def save(item, candidate):
        try:
            barrier.wait(timeout=10)
            try:
                return item.save_generated_scene_snapshot(candidate)
            except GeneratedSceneConflict:
                return "conflict"
        finally:
            item.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, stores, candidates))
    assert results.count("conflict") == 1
    assert sum(value in candidates for value in results if value != "conflict") == 1
