"""Player recovery routes use durable evidence without starting turn work."""

import asyncio
import hashlib
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from httpx2 import AsyncClient as HttpAsyncClient, MockTransport, Response
from openai import AsyncClient as OpenAIAsyncClient
from unittest.mock import AsyncMock
from types import SimpleNamespace

from storyloop_platform.bootstrap import build_portal
from storyloop_platform.config import load_settings
from storyloop_platform.portal.http_api import create_app
from storyloop_platform.portal.billing import record_model_usage
from storyloop_platform.runtime.guidance import GuidanceAdvisor
from test_generation_budget_integration import _completion
from test_turn_failure_contract import ROOT, portal
from test_turn_settlement_recovery import completed_session
from committed_input import commit_player_input


def _save(portal):
    token = portal.register("recovery-reader", "password-123")["token"]
    game_id = asyncio.run(portal.create_save(token, "npc-chat"))["game_id"]
    return token, game_id


def test_status_receipt_replay_by_hash_needs_no_generation_or_preferences(portal, monkeypatch):
    token, game_id = _save(portal)
    text = "你好 🌙"
    request_id = "receipt"
    completed_session(portal)
    asyncio.run(portal.turn(token, game_id, text, request_id))
    fingerprint = hashlib.sha256(text.encode()).hexdigest()

    def forbidden(*args, **kwargs):
        raise AssertionError("status must not start model, preferences, or work")

    monkeypatch.setattr(portal.memory_service, "preferences_for", forbidden)
    monkeypatch.setattr(portal.gameplay.factory, "engine", forbidden)
    monkeypatch.setattr(portal.turns.audits, "start", forbidden)
    response = portal.turn_recovery_status(token, game_id, request_id, fingerprint)
    assert response == dict(request_id=request_id, status="complete", can_recover=True,
                            reason_code="RECEIPT_READY")
    assert portal.turn_recovery_status(token, game_id, request_id, "0" * 64) == dict(
        request_id=request_id, status="blocked", can_recover=False, reason_code="INPUT_MISMATCH")


def test_status_http_rejects_invalid_hash_and_request_id(portal):
    token, game_id = _save(portal)
    with TestClient(create_app(portal), base_url="http://127.0.0.1") as client:
        headers = {"Authorization": f"Bearer {token}"}
        path = f"/v1/saves/{game_id}/turns/receipt/recovery"
        assert client.get(path, params={"input_hash": "abc"}, headers=headers).status_code == 422
        assert client.get(path.replace("/receipt/", "/bad.id/"),
                          params={"input_hash": "0" * 64}, headers=headers).status_code == 422


def test_status_checks_ownership_before_evidence(portal):
    owner, game_id = _save(portal)
    other = portal.register("second-reader", "password-123")["token"]
    with pytest.raises(KeyError):
        portal.turn_recovery_status(other, game_id, "r", "0" * 64)


@pytest.fixture
def native_portal(tmp_path, monkeypatch):
    sends = []
    transport = HttpAsyncClient(transport=MockTransport(lambda request: (
        sends.append(request), _completion('{"prose":"A quiet harbor."}',
                                            prompt_tokens=1200, completion_tokens=600))[1]))
    original = OpenAIAsyncClient.__init__

    def init(client, *args, **kwargs):
        kwargs["http_client"] = transport
        original(client, *args, **kwargs)

    monkeypatch.setattr(OpenAIAsyncClient, "__init__", init)
    monkeypatch.setenv("STORY_UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("STORY_MODEL_API_KEY", "offline-test")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "")
    instance = build_portal(ROOT / "examples/catalog.json", load_settings(ROOT / "config/local.json"),
                            str(tmp_path / "portal.sqlite3"),
                            prologue_generator=AsyncMock(return_value="A quiet opening."))
    try:
        yield instance, sends
    finally:
        instance.close()
        asyncio.run(transport.aclose())


def test_native_fresh_turn_freezes_before_one_sdk_send_and_bills_saved_usage(native_portal):
    portal, sends = native_portal
    token = portal.register("native-reader", "password-123")["token"]
    game_id = asyncio.run(portal.create_save(token, "npc-chat"))["game_id"]
    result = asyncio.run(portal.turn(token, game_id, "look 🌙", "native"))
    context = portal.turns.recovery_contexts.get(game_id, "native")
    saved = portal.gameplay.store.generated_scene_snapshot(game_id, "portal-native")
    assert context is not None and saved is not None
    assert len(sends) == 1
    assert result["billing"]["input_tokens"] == 1200
    assert result["billing"]["model_calls"] == 1


def test_saved_scene_recovers_without_a_second_send_or_repricing(native_portal, monkeypatch):
    portal, sends = native_portal
    token = portal.register("scene-reader", "password-123")["token"]
    game_id = asyncio.run(portal.create_save(token, "npc-chat"))["game_id"]
    original_prepare = portal.turns.settlements.prepare

    def disconnected(*args, **kwargs):
        raise RuntimeError("settlement unavailable")

    monkeypatch.setattr(portal.turns.settlements, "prepare", disconnected)
    with pytest.raises(RuntimeError, match="settlement unavailable"):
        asyncio.run(portal.turn(token, game_id, "listen", "scene"))
    assert len(sends) == 1
    credential = portal.gameplay.factory.models._credential
    monkeypatch.setattr(portal.gameplay.factory.models, "_credential",
                        lambda *args: (_ for _ in ()).throw(AssertionError("GET loaded credential")))
    monkeypatch.setattr(portal.memory_service, "preferences_for",
                        lambda *args: (_ for _ in ()).throw(AssertionError("GET loaded preferences")))
    assert portal.turn_recovery_status(token, game_id, "scene",
            hashlib.sha256(b"listen").hexdigest())["reason_code"] == "SCENE_READY"
    monkeypatch.setattr(portal.gameplay.factory.models, "_credential", credential)
    monkeypatch.setattr(portal.turns.settlements, "prepare", original_prepare)
    frozen_policy = portal.turns.recovery_contexts.get(game_id, "scene").policy()
    portal.turns.billing.policy = replace(frozen_policy, pricing_version="future-rate",
                                          points_per_rmb=999)
    result = asyncio.run(portal.turn(token, game_id, "listen", "scene"))
    assert len(sends) == 1
    assert result["billing"]["input_tokens"] == 1200
    assert portal.turns.settlements.get(game_id, "scene", "listen").policy == frozen_policy
    assert portal.turn_recovery_status(token, game_id, "scene",
            hashlib.sha256(b"listen").hexdigest())["reason_code"] == "RECEIPT_READY"


def test_recovery_keeps_original_paid_terms_after_current_billing_disabled(native_portal, monkeypatch):
    portal, sends = native_portal
    token = portal.register("paid-reader", "password-123")["token"]
    game_id = asyncio.run(portal.create_save(token, "npc-chat"))["game_id"]
    frozen_policy = portal.billing.policy
    original_prepare = portal.turns.settlements.prepare
    monkeypatch.setattr(portal.turns.settlements, "prepare",
                        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline")))
    with pytest.raises(RuntimeError, match="offline"):
        asyncio.run(portal.turn(token, game_id, "wait", "paid"))
    portal.turns.billing = None
    monkeypatch.setattr(portal.turns.settlements, "prepare", original_prepare)
    receipt = asyncio.run(portal.turn(token, game_id, "wait", "paid"))
    assert receipt["billing"]["model_calls"] == 1
    assert portal.turns.settlements.get(game_id, "paid", "wait").policy == frozen_policy
    audits = portal.turns.audits.list_for_request(game_id, "paid")
    assert audits[-1]["policy"] == frozen_policy.to_dict()
    assert audits[-1]["attempts"] == []
    assert len(sends) == 1


def test_recovery_keeps_original_free_terms_after_current_billing_enabled(native_portal, monkeypatch):
    portal, sends = native_portal
    token = portal.register("free-reader", "password-123")["token"]
    game_id = asyncio.run(portal.create_save(token, "npc-chat"))["game_id"]
    paid = portal.turns.billing
    portal.turns.billing = None
    original_store = portal.accounts.store_turn_response
    monkeypatch.setattr(portal.accounts, "store_turn_response",
                        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline")))
    with pytest.raises(RuntimeError, match="offline"):
        asyncio.run(portal.turn(token, game_id, "wait", "free"))
    assert portal.turns.recovery_contexts.get(game_id, "free").billing_enabled is False
    portal.turns.billing = paid
    monkeypatch.setattr(portal.accounts, "store_turn_response", original_store)
    receipt = asyncio.run(portal.turn(token, game_id, "wait", "free"))
    assert "billing" not in receipt
    assert len(sends) == 1


def test_saved_scene_rejects_changed_unicode_input_and_generation_identity(native_portal, monkeypatch):
    portal, sends = native_portal
    token = portal.register("identity-reader", "password-123")["token"]
    game_id = asyncio.run(portal.create_save(token, "npc-chat"))["game_id"]
    original_prepare = portal.turns.settlements.prepare
    monkeypatch.setattr(portal.turns.settlements, "prepare",
                        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline")))
    with pytest.raises(RuntimeError, match="offline"):
        asyncio.run(portal.turn(token, game_id, "é", "identity"))
    monkeypatch.setattr(portal.turns.settlements, "prepare", original_prepare)
    with pytest.raises(ValueError):
        asyncio.run(portal.turn(token, game_id, "e\u0301", "identity"))
    assert portal.turn_recovery_status(token, game_id, "identity",
            hashlib.sha256("e\u0301".encode()).hexdigest())["reason_code"] == "INPUT_MISMATCH"
    original_plan = portal.gameplay.factory.scene_recovery_plan
    monkeypatch.setattr(portal.gameplay.factory, "scene_recovery_plan",
                        lambda *args: replace(original_plan(*args), generation_identity="0" * 64))
    assert portal.turn_recovery_status(token, game_id, "identity",
            hashlib.sha256("é".encode()).hexdigest())["reason_code"] == "CONFIG_CHANGED"
    with pytest.raises(ValueError):
        asyncio.run(portal.turn(token, game_id, "é", "identity"))
    assert len(sends) == 1


def test_freeze_failure_prevents_sdk_send_and_audit(native_portal, monkeypatch):
    portal, sends = native_portal
    token = portal.register("freeze-reader", "password-123")["token"]
    game_id = asyncio.run(portal.create_save(token, "npc-chat"))["game_id"]
    monkeypatch.setattr(portal.turns.recovery_contexts, "freeze",
                        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("freeze offline")))
    with pytest.raises(RuntimeError, match="freeze offline"):
        asyncio.run(portal.turn(token, game_id, "look", "freeze"))
    assert sends == []
    assert portal.turns.audits.list_for_request(game_id, "freeze") == []


def test_prepared_receipt_status_survives_later_request(portal, monkeypatch):
    token, game_id = _save(portal)
    completed_session(portal)
    original_settle = portal.billing.settle_turn
    monkeypatch.setattr(portal.billing, "settle_turn",
                        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("settlement offline")))
    with pytest.raises(RuntimeError, match="settlement offline"):
        asyncio.run(portal.turn(token, game_id, "first", "prepared"))
    fingerprint = hashlib.sha256(b"first").hexdigest()
    assert portal.turn_recovery_status(token, game_id, "prepared", fingerprint)["reason_code"] == "PREPARED_READY"
    monkeypatch.setattr(portal.billing, "settle_turn", original_settle)
    asyncio.run(portal.turn(token, game_id, "first", "prepared"))
    asyncio.run(portal.turn(token, game_id, "second", "later"))
    assert portal.turn_recovery_status(token, game_id, "prepared", fingerprint)["reason_code"] == "RECEIPT_READY"


def test_controlled_memory_queues_only_fresh_completed_input(native_portal, monkeypatch):
    portal, sends = native_portal
    token = portal.register("memory-reader", "password-123")["token"]
    game_id = asyncio.run(portal.create_save(token, "npc-chat"))["game_id"]
    queued = []
    monkeypatch.setattr(portal.memory_service, "queue_input",
                        lambda *args, **kwargs: queued.append((args, kwargs)))
    asyncio.run(portal.turn(token, game_id, "I walk toward the harbor", "first"))
    asyncio.run(portal.turn(token, game_id, "I walk toward the harbor", "first"))
    assert len(queued) == 1 and len(sends) == 1
    original_prepare = portal.turns.settlements.prepare
    monkeypatch.setattr(portal.turns.settlements, "prepare",
                        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline")))
    with pytest.raises(RuntimeError, match="offline"):
        asyncio.run(portal.turn(token, game_id, "I watch the moon rise", "second"))
    monkeypatch.setattr(portal.turns.settlements, "prepare", original_prepare)
    asyncio.run(portal.turn(token, game_id, "I watch the moon rise", "second"))
    assert len(queued) == 1 and len(sends) == 2


def test_precommit_saved_scene_replays_without_second_sdk_send(native_portal, monkeypatch):
    portal, sends = native_portal
    token = portal.register("precommit-reader", "password-123")["token"]
    game_id = asyncio.run(portal.create_save(token, "npc-chat"))["game_id"]
    original_commit = portal.gameplay.store.commit
    stopped = [False]

    def crash_before_commit(*args, **kwargs):
        event = args[2]
        if event.event_id == "portal-precommit:input" and not stopped[0]:
            stopped[0] = True
            raise RuntimeError("process boundary")
        return original_commit(*args, **kwargs)

    monkeypatch.setattr(portal.gameplay.store, "commit", crash_before_commit)
    with pytest.raises(RuntimeError, match="process boundary"):
        asyncio.run(portal.turn(token, game_id, "look", "precommit"))
    assert portal.gameplay.store.generated_scene_snapshot(game_id, "portal-precommit") is not None
    assert not portal.gameplay.store.event_exists(game_id, "portal-precommit:input")
    result = asyncio.run(portal.turn(token, game_id, "look", "precommit"))
    assert result["billing"]["input_tokens"] == 1200
    assert portal.gameplay.store.event_exists(game_id, "portal-precommit:input")
    assert len(sends) == 1


def test_precommit_scene_after_an_earlier_turn_remains_recoverable(native_portal, monkeypatch):
    portal, sends = native_portal
    token = portal.register("second-turn-reader", "password-123")["token"]
    game_id = asyncio.run(portal.create_save(token, "npc-chat"))["game_id"]
    asyncio.run(portal.turn(token, game_id, "first", "first"))
    original_commit = portal.gameplay.store.commit
    failed = [False]

    def crash_once(*args, **kwargs):
        if args[2].event_id == "portal-second:input" and not failed[0]:
            failed[0] = True
            raise RuntimeError("process boundary")
        return original_commit(*args, **kwargs)

    monkeypatch.setattr(portal.gameplay.store, "commit", crash_once)
    with pytest.raises(RuntimeError, match="process boundary"):
        asyncio.run(portal.turn(token, game_id, "second", "second"))
    assert portal.turn_recovery_status(token, game_id, "second",
            hashlib.sha256(b"second").hexdigest())["reason_code"] == "SCENE_READY"
    result = asyncio.run(portal.turn(token, game_id, "second", "second"))
    assert result["billing"]["model_calls"] == 1
    assert len(sends) == 2


def test_newer_pending_context_blocks_older_scene_recovery(native_portal, monkeypatch):
    portal, sends = native_portal
    token = portal.register("later-reader", "password-123")["token"]
    player_id = portal.accounts.resolve_token(token)
    game_id = asyncio.run(portal.create_save(token, "npc-chat"))["game_id"]
    original_prepare = portal.turns.settlements.prepare
    monkeypatch.setattr(portal.turns.settlements, "prepare",
                        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline")))
    with pytest.raises(RuntimeError, match="offline"):
        asyncio.run(portal.turn(token, game_id, "first", "older"))
    monkeypatch.setattr(portal.turns.settlements, "prepare", original_prepare)
    old = portal.turns.recovery_contexts.get(game_id, "older")
    portal.turns.recovery_contexts.freeze(player_id=player_id, game_id=game_id,
        request_id="newer", input_hash=hashlib.sha256(b"second").hexdigest(),
        generation_identity=old.generation_identity, billing_enabled=True,
        policy=old.policy())
    assert portal.turn_recovery_status(token, game_id, "older",
            hashlib.sha256(b"first").hexdigest())["reason_code"] == "LATER_REQUEST"
    with pytest.raises(ValueError):
        asyncio.run(portal.turn(token, game_id, "first", "older"))
    assert len(sends) == 1


def test_later_committed_player_input_blocks_scene_recovery(native_portal, monkeypatch):
    portal, sends = native_portal
    token = portal.register("committed-reader", "password-123")["token"]
    game_id = asyncio.run(portal.create_save(token, "npc-chat"))["game_id"]
    original_prepare = portal.turns.settlements.prepare
    monkeypatch.setattr(portal.turns.settlements, "prepare",
                        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline")))
    with pytest.raises(RuntimeError, match="offline"):
        asyncio.run(portal.turn(token, game_id, "first", "older"))
    monkeypatch.setattr(portal.turns.settlements, "prepare", original_prepare)
    commit_player_input(portal.gameplay.store, game_id, "portal-other:input", "second")
    assert portal.turn_recovery_status(token, game_id, "older",
            hashlib.sha256(b"first").hexdigest())["reason_code"] == "LATER_REQUEST"
    with pytest.raises(ValueError):
        asyncio.run(portal.turn(token, game_id, "first", "older"))
    assert len(sends) == 1


def test_reported_zero_usage_is_valid_and_unpriced_usage_is_blocked(native_portal, monkeypatch):
    portal, sends = native_portal
    token = portal.register("usage-reader", "password-123")["token"]
    game_id = asyncio.run(portal.create_save(token, "npc-chat"))["game_id"]
    monkeypatch.setattr(__import__(__name__), "_completion",
                        lambda arguments, **kwargs: _original_completion(arguments,
                            prompt_tokens=0, completion_tokens=0))
    zero = asyncio.run(portal.turn(token, game_id, "zero", "zero"))
    assert zero["billing"]["model_calls"] == 1
    assert zero["billing"]["input_tokens"] == 0
    assert zero["billing"]["charged_milli_points"] == 0

    other_game = asyncio.run(portal.create_save(token, "npc-chat"))["game_id"]
    policy = portal.billing.policy
    portal.turns.billing.policy = replace(policy, rates={"unrelated": next(iter(policy.rates.values()))})
    with pytest.raises(ValueError):
        asyncio.run(portal.turn(token, other_game, "unpriced", "unpriced"))
    assert len(sends) == 2
    assert portal.turn_recovery_status(token, other_game, "unpriced",
            hashlib.sha256(b"unpriced").hexdigest())["reason_code"] == "USAGE_UNAVAILABLE"


_original_completion = _completion


def test_missing_reported_usage_blocks_before_scene_is_saved(native_portal, monkeypatch):
    portal, sends = native_portal
    token = portal.register("missing-usage-reader", "password-123")["token"]
    game_id = asyncio.run(portal.create_save(token, "npc-chat"))["game_id"]

    def no_usage(arguments, **kwargs):
        payload = _original_completion(arguments).json()
        payload.pop("usage")
        return Response(200, json=payload)

    monkeypatch.setattr(__import__(__name__), "_completion", no_usage)
    with pytest.raises(RuntimeError, match="did not report token usage"):
        asyncio.run(portal.turn(token, game_id, "unknown", "missing"))
    saved = portal.gameplay.store.generated_scene_snapshot(game_id, "portal-missing")
    assert saved is None
    assert portal.turn_recovery_status(token, game_id, "missing",
            hashlib.sha256(b"unknown").hexdigest())["reason_code"] == "NO_SNAPSHOT"
    assert len(sends) == 1


def test_identical_extra_usage_outside_scene_cannot_be_billed(native_portal, monkeypatch):
    portal, sends = native_portal
    token = portal.register("extra-usage-reader", "password-123")["token"]
    game_id = asyncio.run(portal.create_save(token, "npc-chat"))["game_id"]
    original = GuidanceAdvisor.advise

    async def extra_success(self, *args, **kwargs):
        record_model_usage("deepseek-v4.1-flash", "single_turn", SimpleNamespace(
            input_tokens=1200, output_tokens=600, metadata={}))
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(GuidanceAdvisor, "advise", extra_success)
    with pytest.raises(ValueError, match="未覆盖"):
        asyncio.run(portal.turn(token, game_id, "look", "extra"))
    assert portal.gameplay.store.generated_scene_snapshot(game_id, "portal-extra") is not None
    assert portal.accounts.get_turn_response(game_id, "extra", "look") is None
    assert len(sends) == 1
