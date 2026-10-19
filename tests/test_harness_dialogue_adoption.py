"""Upgrade boundaries for the ordered dialogue scene contract."""

import asyncio
import hashlib
import json
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from storyloop_harness import ScenarioPackage, TurnEngine, TurnInput

from storyloop_platform.gameplay import factory as factory_module
from storyloop_platform.portal.turn_errors import TurnGenerationUncertain
from storyloop_platform.bootstrap import build_portal
from storyloop_platform.config import load_settings
import test_player_scene_recovery as recovery_fixture
from test_player_scene_recovery import native_portal
from scenario_fixtures import source_package


def test_old_unfinished_scene_keeps_original_request_and_blocks_resend(native_portal, monkeypatch):
    portal, sends = native_portal
    token = portal.register("dialogue-upgrade-reader", "password-123")["token"]
    game_id = asyncio.run(portal.create_save(token, "npc-chat"))["game_id"]
    request_id = "old-scene"
    player_text = "look"

    with monkeypatch.context() as old_runtime:
        old_runtime.setattr(factory_module, "PLATFORM_SCENE_CONTRACT", "platform-ordinary-scene-v1")
        old_runtime.setattr(portal.turns.settlements, "prepare",
                            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline")))
        with pytest.raises(RuntimeError, match="offline"):
            asyncio.run(portal.turn(token, game_id, player_text, request_id))
    old_context = portal.turns.recovery_contexts.get(game_id, request_id)
    old_snapshot = portal.gameplay.store.generated_scene_snapshot(game_id, "portal-" + request_id)
    before = portal.gameplay.store.load(game_id)
    assert old_context is not None and old_snapshot is not None
    assert len(sends) == 1
    fingerprint = hashlib.sha256(player_text.encode()).hexdigest()
    assert portal.turn_recovery_status(token, game_id, request_id, fingerprint) == {
        "request_id": request_id, "status": "blocked", "can_recover": False,
        "reason_code": "CONFIG_CHANGED"}
    with pytest.raises(ValueError):
        asyncio.run(portal.turn(token, game_id, player_text, request_id))
    assert portal.turns.recovery_contexts.get(game_id, request_id) == old_context
    assert portal.gameplay.store.generated_scene_snapshot(game_id, "portal-" + request_id) == old_snapshot
    assert portal.gameplay.store.load(game_id) == before
    assert portal.turns.settlements.get(game_id, request_id, player_text) is None
    assert len(sends) == 1


def test_completed_old_identity_replays_cached_receipt_after_upgrade(native_portal, monkeypatch):
    portal, sends = native_portal
    token = portal.register("completed-upgrade-reader", "password-123")["token"]
    game_id = asyncio.run(portal.create_save(token, "npc-chat"))["game_id"]
    with monkeypatch.context() as old_runtime:
        old_runtime.setattr(factory_module, "PLATFORM_SCENE_CONTRACT", "platform-ordinary-scene-v1")
        completed = asyncio.run(portal.turn(token, game_id, "look", "completed-old"))
    wallet = portal.wallet(token)
    assert portal.turn_recovery_status(token, game_id, "completed-old",
            hashlib.sha256(b"look").hexdigest())["reason_code"] == "RECEIPT_READY"
    assert asyncio.run(portal.turn(token, game_id, "look", "completed-old")) == completed
    assert portal.wallet(token) == wallet
    assert len(sends) == 1


def test_native_player_segments_follow_beats_once(native_portal, monkeypatch):
    portal, sends = native_portal
    item = portal.gameplay.game_access.listing_for("unused", "npc-chat")
    package = ScenarioPackage.load(item.package_path)
    package = replace(package, initial_state={**package.initial_state,
        "player_knowledge": {"named_actor_ids": [], "seen_actor_ids": []}})
    monkeypatch.setattr(ScenarioPackage, "load", classmethod(lambda cls, path: package))
    token = portal.register("ordered-beats-reader", "password-123")["token"]
    game_id = asyncio.run(portal.create_save(token, "npc-chat"))["game_id"]
    decision = {"beats": [
        {"kind": "narration", "text": "The dockhand sets down a rope."},
        {"kind": "dialogue", "actor_id": "dockhand", "text": "船快到了。"},
        {"kind": "narration", "text": "He points to the arriving boat."},
        {"kind": "dialogue", "actor_id": "dockhand", "text": "船期写在告示上。"},
        {"kind": "dialogue", "actor_id": "dockhand", "text": "请上船。"}],
        "participants": ["dockhand"]}
    monkeypatch.setattr(recovery_fixture, "_completion", lambda _arguments, **kwargs:
                        recovery_fixture._original_completion(json.dumps(decision),
                            prompt_tokens=1200, completion_tokens=600))
    result = asyncio.run(portal.turn(token, game_id, "look", "ordered"))
    segments = result["segments"]
    assert [(part["kind"], part["text"]) for part in segments] == [
        ("scene", "The dockhand sets down a rope."),
        ("dialogue", "船快到了。"),
        ("scene", "He points to the arriving boat."),
        ("dialogue", "船期写在告示上。"),
        ("dialogue", "请上船。")]
    assert [part["speaker_name"] for part in segments if part["kind"] == "dialogue"] == [
        "一位嘉宾", "一位嘉宾", "一位嘉宾"]
    assert result["body"].count("船快到了。") == 1
    assert result["body"].count("船期写在告示上。") == 1
    assert len(sends) == 1


def test_native_source_scene_reveals_name_only_after_introduction(native_portal, monkeypatch):
    portal, sends = native_portal
    package = source_package()
    package = replace(package, initial_state={**package.initial_state,
        "player_knowledge": {"named_actor_ids": [], "seen_actor_ids": []}})
    game_id = "native-source-introduction"
    package.seed_game(portal.gameplay.store, game_id)
    decision = {"beats": [
        {"kind": "dialogue", "actor_id": "dockhand", "text": "The boat is coming."},
        {"kind": "dialogue", "actor_id": "dockhand", "text": "我叫Dockhand。"},
        {"kind": "dialogue", "actor_id": "dockhand", "text": "Welcome aboard."}],
        "participants": ["dockhand"],
        "options": [{"label": "Ask", "input": "Ask about the boat"},
                    {"label": "Look", "input": "Look at the water"},
                    {"label": "Board", "input": "Board the boat"}],
        "encounters": [{"actor_id": "dockhand", "evidence": "我叫Dockhand。", "name_learned": True}]}
    monkeypatch.setattr(recovery_fixture, "_completion", lambda _arguments, **kwargs:
                        recovery_fixture._original_completion(json.dumps(decision),
                            prompt_tokens=1200, completion_tokens=600))
    model = portal.gameplay.factory.models.create_model("single_turn")
    outcome = asyncio.run(TurnEngine(portal.gameplay.store, package, model).run_turn(
        TurnInput(game_id, "Hello", "introduction", package.version)))
    assert [part.speaker_name for part in outcome.segments if part.kind == "dialogue"] == [
        "一位嘉宾", "一位嘉宾", "Dockhand"]
    assert len(sends) == 1


def test_native_duplicate_answer_is_one_send_with_no_world_or_billing_commit(native_portal, monkeypatch):
    portal, sends = native_portal
    token = portal.register("duplicate-answer-reader", "password-123")["token"]
    game_id = asyncio.run(portal.create_save(token, "npc-chat"))["game_id"]
    wallet = portal.wallet(token)
    before = portal.gameplay.store.load(game_id)
    decision = {"beats": [
        {"kind": "narration", "text": "The dockhand says the morning boat is due soon."},
        {"kind": "dialogue", "actor_id": "dockhand", "text": "The morning boat is due soon."}],
        "participants": ["dockhand"]}
    monkeypatch.setattr(recovery_fixture, "_completion", lambda _arguments, **kwargs:
                        recovery_fixture._original_completion(json.dumps(decision),
                            prompt_tokens=1200, completion_tokens=600))
    with pytest.raises(TurnGenerationUncertain):
        asyncio.run(portal.turn(token, game_id, "look", "duplicate"))
    assert len(sends) == 1
    assert portal.gameplay.store.load(game_id) == before
    assert portal.wallet(token) == wallet
    assert portal.gameplay.store.generated_scene_snapshot(game_id, "portal-duplicate") is None
    audit = portal.turns.audits.list_for_request(game_id, "duplicate")[0]
    assert audit["summary"]["known_input_tokens"] == 1200
    assert audit["summary"]["known_output_tokens"] == 600


def test_old_uncertain_marker_is_preserved_and_blocked_after_upgrade(native_portal, monkeypatch):
    portal, sends = native_portal
    token = portal.register("marker-upgrade-reader", "password-123")["token"]
    game_id = asyncio.run(portal.create_save(token, "npc-chat"))["game_id"]
    monkeypatch.setattr(recovery_fixture, "_completion", lambda _arguments, **kwargs:
                        recovery_fixture._original_completion("{}"))
    with monkeypatch.context() as old_runtime:
        old_runtime.setattr(factory_module, "PLATFORM_SCENE_CONTRACT", "platform-ordinary-scene-v1")
        with pytest.raises(TurnGenerationUncertain):
            asyncio.run(portal.turn(token, game_id, "look", "old-marker"))
    marker = portal.gameplay.store.generated_scene_generation_marker(game_id, "portal-old-marker")
    context = portal.turns.recovery_contexts.get(game_id, "old-marker")
    assert marker is not None and context is not None
    assert portal.turn_recovery_status(token, game_id, "old-marker",
            hashlib.sha256(b"look").hexdigest())["reason_code"] == "CONFIG_CHANGED"
    with pytest.raises(ValueError):
        asyncio.run(portal.turn(token, game_id, "look", "old-marker"))
    assert portal.gameplay.store.generated_scene_generation_marker(game_id, "portal-old-marker") == marker
    assert portal.turns.recovery_contexts.get(game_id, "old-marker") == context
    assert len(sends) == 1


def test_new_scene_recovers_after_sqlite_reopen_at_original_rate(native_portal, monkeypatch, tmp_path):
    portal, sends = native_portal
    token = portal.register("reopened-scene-reader", "password-123")["token"]
    game_id = asyncio.run(portal.create_save(token, "npc-chat"))["game_id"]
    wallet_before = portal.wallet(token)
    with monkeypatch.context() as interrupted:
        interrupted.setattr(portal.turns.settlements, "prepare",
                            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline")))
        with pytest.raises(RuntimeError, match="offline"):
            asyncio.run(portal.turn(token, game_id, "look", "reopened"))
    context = portal.turns.recovery_contexts.get(game_id, "reopened")
    assert context is not None
    frozen_policy = context.policy()
    assert portal.gameplay.store.generated_scene_snapshot(game_id, "portal-reopened") is not None
    assert len(sends) == 1

    reopened = build_portal(recovery_fixture.ROOT / "examples/catalog.json",
                            load_settings(recovery_fixture.ROOT / "config/local.json"),
                            str(tmp_path / "portal.sqlite3"),
                            prologue_generator=AsyncMock(return_value="A quiet opening."))
    try:
        reopened.turns.billing.policy = replace(frozen_policy, pricing_version="future-rate",
                                                points_per_rmb=999)
        assert reopened.turn_recovery_status(token, game_id, "reopened",
                hashlib.sha256(b"look").hexdigest())["reason_code"] == "SCENE_READY"
        result = asyncio.run(reopened.turn(token, game_id, "look", "reopened"))
        assert result["billing"]["model_calls"] == 1
        assert result["billing"]["input_tokens"] == 1200
        assert reopened.turns.settlements.get(game_id, "reopened", "look").policy == frozen_policy
        assert len([entry for entry in reopened.credit_ledger(token) if entry["kind"] == "turn"]) == 1
        assert reopened.wallet(token)["balance_milli_points"] < wallet_before["balance_milli_points"]
        assert asyncio.run(reopened.turn(token, game_id, "look", "reopened")) == result
        assert len([entry for entry in reopened.credit_ledger(token) if entry["kind"] == "turn"]) == 1
        assert len(sends) == 1
    finally:
        reopened.close()
