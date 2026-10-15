"""Durable generation claims are immutable and fail closed on damaged evidence."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from unittest.mock import patch

import pytest
from sqlalchemy import text

from storyloop_harness.advanced import (
    GeneratedSceneCorrupt, GeneratedSceneGenerationMarker, Snapshot,
)
from storyloop_platform.adapters.store import SQLiteGameStore


def marker(**changes):
    values = dict(game_id="game", turn_id="turn", input_hash="a" * 64,
                  base_version=0, base_tick=0, base_state_hash="b" * 64,
                  scenario_id="harbor", scenario_version="1", package_fingerprint="c" * 64,
                  system_prompt_hash="d" * 64, output_schema_hash="e" * 64,
                  commit_config_hash="f" * 64, generation_config_hash="1" * 64)
    return GeneratedSceneGenerationMarker.create(**(values | changes))


@pytest.fixture
def store(tmp_path):
    item = SQLiteGameStore(str(tmp_path / "game.db"))
    item.create_game(Snapshot("game", 0, 0, {"world": {"flag": False}}))
    yield item
    item.close()


def test_claim_reopen_and_occupied_claim_do_not_change_world(store):
    before = store.load("game")
    first = marker()
    assert store.generated_scene_generation_marker("game", "turn") is None
    assert store.create_generated_scene_generation_marker(first) is True
    assert store.create_generated_scene_generation_marker(first) is False
    assert store.create_generated_scene_generation_marker(marker(input_hash="9" * 64)) is False
    reopened = SQLiteGameStore(store.path)
    try:
        assert reopened.generated_scene_generation_marker("game", "turn") == first
    finally:
        reopened.close()
    assert store.load("game") == before
    assert store.player_inputs_for("game") == []
    assert store.pending_work("game") == []


@pytest.mark.parametrize("field,value", [("checksum", "0" * 64), ("game_id", "other"),
                                         ("turn_id", "other"), ("marker_id", "0" * 64)])
def test_wrapper_damage_rejected(store, field, value):
    with pytest.raises(GeneratedSceneCorrupt):
        store.create_generated_scene_generation_marker(replace(marker(), **{field: value}))
    assert store.generated_scene_generation_marker("game", "turn") is None


@pytest.mark.parametrize("field,value", [("checksum", "0" * 64), ("marker_id", "0" * 64),
                                         ("canonical_json", marker(game_id="other").canonical_json),
                                         ("canonical_json", marker(turn_id="other").canonical_json)])
def test_stored_row_damage_rejected_by_read_and_second_claim(store, field, value):
    assert store.create_generated_scene_generation_marker(marker())
    with store.engine.begin() as db:
        db.execute(text(f"UPDATE generated_scene_generation_markers SET {field}=:value"),
                   {"value": value})
    with pytest.raises(GeneratedSceneCorrupt):
        store.generated_scene_generation_marker("game", "turn")
    with pytest.raises(GeneratedSceneCorrupt):
        store.create_generated_scene_generation_marker(marker())


def test_nonexistent_game_does_not_create_claim(store):
    with pytest.raises(KeyError, match="other"):
        store.create_generated_scene_generation_marker(marker(game_id="other"))
    assert store.generated_scene_generation_marker("other", "turn") is None


def test_game_deletion_cascades_claim(store):
    assert store.create_generated_scene_generation_marker(marker())
    with store.engine.begin() as db:
        db.exec_driver_sql("PRAGMA foreign_keys=ON")
        db.execute(text("DELETE FROM games WHERE game_id='game'"))
    assert store.generated_scene_generation_marker("game", "turn") is None


@pytest.mark.parametrize("foreign", [marker(game_id="other"), marker(turn_id="other")])
def test_valid_foreign_payload_still_rejected_by_row_identity(store, foreign):
    assert store.create_generated_scene_generation_marker(marker())
    with store.engine.begin() as db:
        db.execute(text("""UPDATE generated_scene_generation_markers
            SET marker_id=:marker_id,canonical_json=:canonical_json,checksum=:checksum"""),
            {"marker_id": foreign.marker_id, "canonical_json": foreign.canonical_json,
             "checksum": foreign.checksum})
    with pytest.raises(GeneratedSceneCorrupt, match="row identity mismatch"):
        store.generated_scene_generation_marker("game", "turn")
    with pytest.raises(GeneratedSceneCorrupt, match="row identity mismatch"):
        store.create_generated_scene_generation_marker(marker())


def test_insert_error_rolls_back_and_propagates(store):
    from sqlalchemy.exc import OperationalError

    original = store.engine.dialect.do_execute

    def fail_insert(cursor, statement, parameters, context=None):
        if "INSERT INTO generated_scene_generation_markers" in statement:
            raise OperationalError(statement, parameters, Exception("injected insert error"))
        return original(cursor, statement, parameters, context)

    with patch.object(store.engine.dialect, "do_execute", side_effect=fail_insert):
        with pytest.raises(OperationalError, match="injected insert error"):
            store.create_generated_scene_generation_marker(marker())
    assert store.generated_scene_generation_marker("game", "turn") is None


def test_commit_error_never_acknowledges_success(store):
    from sqlalchemy.exc import OperationalError

    original = store.engine.dialect.do_commit

    def fail_commit(connection):
        raise OperationalError("COMMIT", None, Exception("injected commit error"))

    with patch.object(store.engine.dialect, "do_commit", side_effect=fail_commit):
        with pytest.raises(OperationalError, match="injected commit error"):
            store.create_generated_scene_generation_marker(marker())
    assert store.generated_scene_generation_marker("game", "turn") is None

    def acknowledge_lost(connection):
        original(connection)
        raise OperationalError("COMMIT", None, Exception("acknowledgment lost"))

    with patch.object(store.engine.dialect, "do_commit", side_effect=acknowledge_lost):
        with pytest.raises(OperationalError, match="acknowledgment lost"):
            store.create_generated_scene_generation_marker(marker())
    reopened = SQLiteGameStore(store.path)
    try:
        assert reopened.generated_scene_generation_marker("game", "turn") == marker()
        assert reopened.create_generated_scene_generation_marker(marker()) is False
    finally:
        reopened.close()


def test_two_connections_claim_only_once(store):
    barrier = Barrier(2)
    connections = (SQLiteGameStore(store.path), SQLiteGameStore(store.path))

    def claim(connection):
        try:
            barrier.wait(timeout=10)
            return connection.create_generated_scene_generation_marker(marker())
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(claim, connections)) == [False, True]
