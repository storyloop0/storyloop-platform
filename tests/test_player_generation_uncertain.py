"""A durable generation claim prevents an ordinary player from sending twice."""

import asyncio
import hashlib
from dataclasses import replace

import pytest
from httpx2 import Response
from sqlalchemy import text as sql_text
from sqlalchemy import event
from openai import AsyncClient as OpenAIAsyncClient
from fastapi.testclient import TestClient
from storyloop_platform.portal.http_api import create_app
import json

from test_player_scene_recovery import native_portal, _save
from test_attempt_audit_integration import BlockingUsageStream, sdk_model
from storyloop_harness.generation import GenerationTimeout
from storyloop_platform.portal.turn_errors import TurnGenerationUncertain, TurnRecoveryRequired
from storyloop_platform.gameplay import turns as turns_module
from storyloop_platform.runtime.guidance import GuidanceAdvisor
from committed_input import commit_player_input


_OPENAI_INIT = OpenAIAsyncClient.__init__


def test_first_failure_and_same_request_are_uncertain(native_portal, monkeypatch):
    portal, sends = native_portal
    token, game_id = _save(portal)
    action, request_id = "look at the harbor", "uncertain"
    original = RuntimeError("private snapshot write failure")

    def fail_save(snapshot):
        raise original

    monkeypatch.setattr(portal.turns.store, "save_generated_scene_snapshot", fail_save)
    with pytest.raises(TurnGenerationUncertain) as caught:
        asyncio.run(portal.turn(token, game_id, action, request_id))
    cause = caught.value
    chain = []
    while cause is not None:
        chain.append(cause)
        cause = cause.__cause__
    assert original in chain
    assert len(sends) == 1
    assert portal.turn_recovery_status(token, game_id, request_id,
            hashlib.sha256(action.encode()).hexdigest()) == dict(
                request_id=request_id, status="blocked", can_recover=False,
                reason_code="GENERATION_UNCERTAIN")
    with pytest.raises(TurnGenerationUncertain):
        asyncio.run(portal.turn(token, game_id, action, request_id))
    assert len(sends) == 1


def test_marker_without_context_is_invalid(native_portal, monkeypatch):
    portal, sends = native_portal
    token, game_id = _save(portal)
    action, request_id = "look at the harbor", "orphan"
    monkeypatch.setattr(portal.turns.store, "save_generated_scene_snapshot",
                        lambda snapshot: (_ for _ in ()).throw(RuntimeError("write failure")))
    with pytest.raises(TurnGenerationUncertain):
        asyncio.run(portal.turn(token, game_id, action, request_id))
    with portal.accounts.engine.begin() as db:
        db.execute(sql_text("DELETE FROM turn_recovery_contexts WHERE game_id=:game_id"),
                   {"game_id": game_id})
    assert portal.turn_recovery_status(token, game_id, request_id,
            hashlib.sha256(action.encode()).hexdigest())["reason_code"] == "EVIDENCE_INVALID"
    with pytest.raises(TurnRecoveryRequired):
        asyncio.run(portal.turn(token, game_id, action, request_id))
    assert len(sends) == 1
    assert portal.turns.recovery_contexts.get(game_id, request_id) is None


def test_orphan_retry_has_no_side_effects(native_portal, monkeypatch):
    portal, sends = native_portal
    token, game_id = _save(portal)
    monkeypatch.setattr(portal.turns.store, "save_generated_scene_snapshot",
                        lambda snapshot: (_ for _ in ()).throw(RuntimeError("write failure")))
    with pytest.raises(TurnGenerationUncertain):
        asyncio.run(portal.turn(token, game_id, "look", "orphan-safe"))
    with portal.accounts.engine.begin() as db:
        db.execute(sql_text("DELETE FROM turn_recovery_contexts WHERE game_id=:game"),
                   {"game": game_id})
    tables = ("games", "events", "observations", "pending_work", "turn_recovery_contexts",
              "generated_scene_generation_markers", "generated_scene_snapshots",
              "model_execution_audits", "turn_settlement_intents", "portal_turns",
              "credit_ledger")

    def durable_rows():
        with portal.accounts.engine.connect() as db:
            return {table: tuple(db.execute(sql_text(
                f"SELECT * FROM {table} WHERE game_id=:game"), {"game": game_id}).all())
                for table in tables}

    before = durable_rows()
    wallet = portal.wallet(token)
    ledger = portal.credit_ledger(token)
    def forbidden(*args, **kwargs):
        raise AssertionError("orphan retry started new work")
    monkeypatch.setattr(portal.memory_service, "preferences_for", forbidden)
    monkeypatch.setattr(portal.turns.audits, "start", forbidden)
    monkeypatch.setattr(portal.turns.recovery_contexts, "freeze", forbidden)
    with pytest.raises(TurnRecoveryRequired):
        asyncio.run(portal.turn(token, game_id, "look", "orphan-safe"))
    assert durable_rows() == before
    assert portal.wallet(token) == wallet
    assert portal.credit_ledger(token) == ledger
    assert len(sends) == 1


@pytest.mark.parametrize("damage", ["checksum", "identity"])
def test_corrupt_native_marker_is_invalid(native_portal, monkeypatch, damage):
    portal, sends = native_portal
    token, game_id = _save(portal)
    monkeypatch.setattr(portal.turns.store, "save_generated_scene_snapshot",
                        lambda snapshot: (_ for _ in ()).throw(RuntimeError("write failure")))
    with pytest.raises(TurnGenerationUncertain):
        asyncio.run(portal.turn(token, game_id, "look", "corrupt"))
    with portal.accounts.engine.begin() as db:
        db.execute(sql_text("UPDATE generated_scene_generation_markers SET " +
            ("checksum=:bad" if damage == "checksum" else "marker_id=:bad") +
            " WHERE game_id=:game"), {"bad": "0" * 64, "game": game_id})
    assert portal.turn_recovery_status(token, game_id, "corrupt",
            hashlib.sha256(b"look").hexdigest())["reason_code"] == "EVIDENCE_INVALID"
    with pytest.raises(TurnRecoveryRequired):
        asyncio.run(portal.turn(token, game_id, "look", "corrupt"))
    assert len(sends) == 1


def test_uncertain_marker_binding_precedes_generation(native_portal, monkeypatch):
    portal, sends = native_portal
    token, game_id = _save(portal)
    monkeypatch.setattr(portal.turns.store, "save_generated_scene_snapshot",
                        lambda snapshot: (_ for _ in ()).throw(RuntimeError("write failure")))
    with pytest.raises(TurnGenerationUncertain):
        asyncio.run(portal.turn(token, game_id, "first", "bound"))
    assert portal.turn_recovery_status(token, game_id, "bound",
            hashlib.sha256(b"other").hexdigest())["reason_code"] == "INPUT_MISMATCH"
    original_plan = portal.gameplay.factory.scene_recovery_plan
    monkeypatch.setattr(portal.gameplay.factory, "scene_recovery_plan",
                        lambda *args: replace(original_plan(*args), generation_identity="0" * 64))
    assert portal.turn_recovery_status(token, game_id, "bound",
            hashlib.sha256(b"first").hexdigest())["reason_code"] == "CONFIG_CHANGED"
    with pytest.raises(TurnRecoveryRequired):
        asyncio.run(portal.turn(token, game_id, "first", "bound"))
    assert len(sends) == 1


def test_later_request_blocks_uncertain_marker_replay(native_portal, monkeypatch):
    portal, sends = native_portal
    token, game_id = _save(portal)
    monkeypatch.setattr(portal.turns.store, "save_generated_scene_snapshot",
                        lambda snapshot: (_ for _ in ()).throw(RuntimeError("write failure")))
    with pytest.raises(TurnGenerationUncertain):
        asyncio.run(portal.turn(token, game_id, "first", "older-claim"))
    old = portal.turns.recovery_contexts.get(game_id, "older-claim")
    portal.turns.recovery_contexts.freeze(
        player_id=old.player_id, game_id=game_id, request_id="newer-claim",
        input_hash=hashlib.sha256(b"second").hexdigest(),
        generation_identity=old.generation_identity,
        billing_enabled=old.billing_enabled, policy=old.policy())
    assert portal.turn_recovery_status(token, game_id, "older-claim",
            hashlib.sha256(b"first").hexdigest())["reason_code"] == "LATER_REQUEST"
    with pytest.raises(TurnRecoveryRequired):
        asyncio.run(portal.turn(token, game_id, "first", "older-claim"))
    assert len(sends) == 1


@pytest.mark.parametrize("priority", ["scene", "receipt"])
def test_qualified_result_precedes_corrupt_marker(native_portal, monkeypatch, priority):
    portal, sends = native_portal
    token, game_id = _save(portal)
    if priority == "scene":
        original_prepare = portal.turns.settlements.prepare
        monkeypatch.setattr(portal.turns.settlements, "prepare",
                            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline")))
        with pytest.raises(RuntimeError, match="offline"):
            asyncio.run(portal.turn(token, game_id, "look", "priority"))
        monkeypatch.setattr(portal.turns.settlements, "prepare", original_prepare)
    else:
        asyncio.run(portal.turn(token, game_id, "look", "priority"))
    with portal.accounts.engine.begin() as db:
        db.execute(sql_text("UPDATE generated_scene_generation_markers SET checksum=:bad "
                            "WHERE game_id=:game"), {"bad": "0" * 64, "game": game_id})
    expected = "SCENE_READY" if priority == "scene" else "RECEIPT_READY"
    assert portal.turn_recovery_status(token, game_id, "priority",
            hashlib.sha256(b"look").hexdigest())["reason_code"] == expected
    result = asyncio.run(portal.turn(token, game_id, "look", "priority"))
    assert result["billing"]["model_calls"] == 1
    assert len(sends) == 1
    assert len([row for row in portal.credit_ledger(token) if row["kind"] == "turn"]) == 1


def test_native_timeout_keeps_measured_usage_and_original_audit(native_portal, monkeypatch):
    portal, sends = native_portal
    token, game_id = _save(portal)
    requests = []
    stream = BlockingUsageStream(1000, 500)

    async def reply(request):
        requests.append(request)
        return Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

    monkeypatch.setattr(OpenAIAsyncClient, "__init__", _OPENAI_INIT)
    model, transport = sdk_model(monkeypatch, reply, timeout_seconds=0.3)
    model.stream = True
    monkeypatch.setattr(portal.gameplay.factory.models, "create_resolved_model",
                        lambda **kwargs: model)
    try:
        with pytest.raises(TurnGenerationUncertain) as caught:
            asyncio.run(asyncio.wait_for(portal.turn(token, game_id, "look", "timeout"), 10))
    finally:
        asyncio.run(model.client.close())
        asyncio.run(transport.aclose())
    assert isinstance(caught.value.__cause__, GenerationTimeout)
    audits = portal.turns.audits.list_for_request(game_id, "timeout")
    assert len(audits) == 1
    audit = audits[0]
    assert audit["outcome"] == "failed"
    assert audit["error_type"] == "GenerationTimeout"
    assert audit["attempts"][0]["outcome"] == "failed"
    assert audit["attempts"][0]["usage_state"] == "reported"
    assert audit["attempts"][0]["stream_complete"] is False
    assert audit["summary"]["known_input_tokens"] == 1000
    assert audit["summary"]["known_output_tokens"] == 500
    assert audit["summary"]["total_cost_milli_points"] is None
    assert portal.turn_recovery_status(token, game_id, "timeout",
            hashlib.sha256(b"look").hexdigest())["reason_code"] == "GENERATION_UNCERTAIN"
    with pytest.raises(TurnGenerationUncertain):
        asyncio.run(portal.turn(token, game_id, "look", "timeout"))
    assert portal.turns.audits.list_for_request(game_id, "timeout") == audits
    assert len(requests) == 1
    assert stream.waiting.is_set()
    assert stream.closed
    assert sends == []


@pytest.mark.parametrize("failure_point", ["guidance", "view"])
def test_recovered_scene_failure_remains_recoverable(native_portal, monkeypatch, failure_point):
    portal, sends = native_portal
    token, game_id = _save(portal)
    if failure_point == "guidance":
        async def failed_guidance(*args, **kwargs):
            raise RuntimeError("guidance offline")
        with monkeypatch.context() as fault:
            fault.setattr(GuidanceAdvisor, "advise", failed_guidance)
            with pytest.raises(RuntimeError, match="guidance offline"):
                asyncio.run(portal.turn(token, game_id, "look", "recovered"))
    else:
        with monkeypatch.context() as fault:
            fault.setattr(turns_module, "turn_view",
                          lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("view offline")))
            with pytest.raises(RuntimeError, match="view offline"):
                asyncio.run(portal.turn(token, game_id, "look", "recovered"))
    assert portal.turn_recovery_status(token, game_id, "recovered",
            hashlib.sha256(b"look").hexdigest())["reason_code"] == "SCENE_READY"
    result = asyncio.run(portal.turn(token, game_id, "look", "recovered"))
    assert result["billing"]["model_calls"] == 1
    assert len(sends) == 1
    assert len([row for row in portal.credit_ledger(token) if row["kind"] == "turn"]) == 1


def test_historical_request_without_marker_keeps_legacy_block(native_portal):
    portal, sends = native_portal
    token, game_id = _save(portal)
    assert portal.turn_recovery_status(token, game_id, "historical",
            hashlib.sha256(b"look").hexdigest())["reason_code"] == "NO_SNAPSHOT"
    commit_player_input(portal.gameplay.store, game_id, "portal-historical:input", "look")
    assert portal.gameplay.store.generated_scene_generation_marker(
        game_id, "portal-historical") is None
    assert portal.turn_recovery_status(token, game_id, "historical",
            hashlib.sha256(b"look").hexdigest())["reason_code"] == "LEGACY_REQUEST"
    with pytest.raises(TurnRecoveryRequired):
        asyncio.run(portal.turn(token, game_id, "look", "historical"))
    assert sends == []


def test_inspection_is_read_only(native_portal, monkeypatch):
    portal, sends = native_portal
    token, game_id = _save(portal)
    action, request_id = "look", "readonly"
    monkeypatch.setattr(portal.turns.store, "save_generated_scene_snapshot",
                        lambda snapshot: (_ for _ in ()).throw(RuntimeError("write failure")))
    with pytest.raises(TurnGenerationUncertain):
        asyncio.run(portal.turn(token, game_id, action, request_id))

    def forbidden(*args, **kwargs):
        raise AssertionError("inspection attempted execution")

    monkeypatch.setattr(OpenAIAsyncClient, "__init__", forbidden)
    monkeypatch.setattr(portal.gameplay.factory, "require_credentials", forbidden)
    monkeypatch.setattr(portal.memory_service, "preferences_for", forbidden)
    monkeypatch.setattr(portal.turns.audits, "start", forbidden)
    monkeypatch.setattr(portal.turns.recovery_contexts, "freeze", forbidden)
    statements = []

    def trace(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)
        assert statement.lstrip().upper().startswith("SELECT")

    event.listen(portal.accounts.engine, "before_cursor_execute", trace)
    try:
        result = portal.turn_recovery_status(token, game_id, request_id,
                                             hashlib.sha256(action.encode()).hexdigest())
    finally:
        event.remove(portal.accounts.engine, "before_cursor_execute", trace)
    assert result["reason_code"] == "GENERATION_UNCERTAIN"
    assert statements
    assert len(sends) == 1


def test_secondary_inspection_failure_preserves_original(native_portal, monkeypatch):
    portal, sends = native_portal
    token, game_id = _save(portal)
    original = RuntimeError("original scene write")
    monkeypatch.setattr(portal.turns.store, "save_generated_scene_snapshot",
                        lambda snapshot: (_ for _ in ()).throw(original))
    monkeypatch.setattr(portal.turns, "recovery_status",
                        lambda *args: (_ for _ in ()).throw(RuntimeError("secondary read")))
    with pytest.raises(Exception) as caught:
        asyncio.run(portal.turn(token, game_id, "look", "secondary"))
    assert not isinstance(caught.value, TurnGenerationUncertain)
    assert caught.value.__cause__ is original
    assert len(sends) == 1


def test_cancelled_generation_stays_cancelled(native_portal, monkeypatch):
    portal, sends = native_portal
    token, game_id = _save(portal)
    cancellation = asyncio.CancelledError("original cancellation")

    async def cancelled_model(*args, **kwargs):
        raise cancellation

    monkeypatch.setattr(portal.gameplay.factory.models, "create_resolved_model",
                        lambda **kwargs: cancelled_model)
    with pytest.raises(asyncio.CancelledError) as caught:
        asyncio.run(portal.turn(token, game_id, "look", "cancelled"))
    assert caught.value is cancellation
    assert portal.turn_recovery_status(token, game_id, "cancelled",
            hashlib.sha256(b"look").hexdigest())["reason_code"] == "GENERATION_UNCERTAIN"
    rows = portal.turns.audits.list_for_request(game_id, "cancelled")
    assert len(rows) == 1
    assert rows[0]["outcome"] == "cancelled"
    assert rows[0]["error_type"] == "CancelledError"
    assert len(sends) == 0


def test_plan_fallback_with_existing_marker_cannot_generate(native_portal, monkeypatch):
    portal, sends = native_portal
    token, game_id = _save(portal)
    monkeypatch.setattr(portal.turns.store, "save_generated_scene_snapshot",
                        lambda snapshot: (_ for _ in ()).throw(RuntimeError("write failure")))
    with pytest.raises(TurnGenerationUncertain):
        asyncio.run(portal.turn(token, game_id, "look", "fallback"))
    monkeypatch.setattr(portal.gameplay.factory, "scene_recovery_plan", lambda *args: None)
    monkeypatch.setattr(portal.gameplay.factory, "engine",
                        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("old route")))
    with pytest.raises(TurnRecoveryRequired):
        asyncio.run(portal.turn(token, game_id, "look", "fallback"))
    assert len(sends) == 1


def test_original_write_failure_is_recorded_before_translation(native_portal, monkeypatch):
    portal, _ = native_portal
    token, game_id = _save(portal)
    monkeypatch.setattr(portal.turns.store, "save_generated_scene_snapshot",
                        lambda snapshot: (_ for _ in ()).throw(RuntimeError("write failure")))
    with pytest.raises(TurnGenerationUncertain) as caught:
        asyncio.run(portal.turn(token, game_id, "look", "audit"))
    rows = portal.turns.audits.list_for_request(game_id, "audit")
    assert len(rows) == 1
    assert rows[0]["outcome"] == "failed"
    assert rows[0]["error_type"] == type(caught.value.__cause__).__name__
    assert rows[0]["error_type"] != "TurnGenerationUncertain"
    assert rows[0]["summary"]["known_input_tokens"] > 0


@pytest.mark.parametrize("stream", [False, True])
def test_uncertain_http_first_failure_and_replay(native_portal, monkeypatch, stream):
    portal, sends = native_portal
    token, game_id = _save(portal)
    monkeypatch.setattr(portal.turns.store, "save_generated_scene_snapshot",
                        lambda snapshot: (_ for _ in ()).throw(RuntimeError("private write failure")))
    path = f"/v1/saves/{game_id}/turns" + ("/stream" if stream else "")
    with TestClient(create_app(portal), base_url="http://127.0.0.1") as client:
        for _ in range(2):
            response = client.post(path, headers={"Authorization": f"Bearer {token}"},
                                   json={"text": "look", "request_id": "wire"})
            assert response.status_code == (200 if stream else 409)
            if stream:
                events = [json.loads(line[6:]) for line in response.text.splitlines()
                          if line.startswith("data: ")]
                payload = events[-1]
                assert payload["type"] == "error"
            else:
                payload = response.json()
            assert payload["code"] == "GENERATION_UNCERTAIN"
            assert payload["retryable"] is False
            assert payload["commit_state"] == "unknown"
            assert payload["request_id"] == "wire"
            assert "private" not in response.text
    assert len(sends) == 1
