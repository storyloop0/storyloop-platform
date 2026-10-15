"""A durable generation claim prevents an ordinary player from sending twice."""

import asyncio
import hashlib

import pytest
from sqlalchemy import text as sql_text
from sqlalchemy import event
from openai import AsyncClient as OpenAIAsyncClient
from fastapi.testclient import TestClient
from storyloop_platform.portal.http_api import create_app
import json

from test_player_scene_recovery import native_portal, _save
from storyloop_platform.portal.turn_errors import TurnGenerationUncertain, TurnRecoveryRequired


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
