"""Only failures known to precede execution may discard a pending turn."""

import asyncio
import json
from unittest.mock import AsyncMock
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from storyloop_platform.portal.http_api import create_app
from storyloop_platform.config import load_settings
from storyloop_platform.bootstrap import build_portal
from storyloop_platform.portal.turn_errors import turn_failure, TurnGenerationUncertain
from storyloop_harness.generation import GenerationRequestLimitExceeded, GenerationTimeout
from runtime_fakes import OfflineRuntimeFactory
from committed_input import commit_player_input
from test_scenario_lifecycle_concurrency import archive


ROOT = Path(__file__).resolve().parents[1]


def test_uncertain_failure_overrides_unsafe_handler_copy():
    assert turn_failure(TurnGenerationUncertain(), "same", message="unsafe",
                        code="MODEL_UNAVAILABLE") == {
        "code": "GENERATION_UNCERTAIN",
        "message": "这条行动的生成结果暂时无法确认，已停止重试。请保留此请求并联系管理员。",
        "retryable": False, "commit_state": "unknown", "request_id": "same"}


@pytest.mark.parametrize("error,code,message", [
    (GenerationRequestLimitExceeded(), "GENERATION_REQUEST_LIMIT_EXCEEDED", "Structured generation request limit exceeded"),
    (GenerationTimeout(), "GENERATION_TIMEOUT", "Structured generation timeout exceeded"),
])
def test_generation_control_failure_is_fixed_and_safe(error, code, message):
    failure = turn_failure(error, "request-1", message="private-provider-sentinel")
    assert failure == {"code": code, "message": message, "retryable": False,
                       "commit_state": "unknown", "request_id": "request-1"}


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("error_type,code", [
    (GenerationRequestLimitExceeded, "GENERATION_REQUEST_LIMIT_EXCEEDED"),
    (GenerationTimeout, "GENERATION_TIMEOUT"),
])
def test_generation_control_http_error_is_safe(portal, stream, error_type, code):
    token = portal.register("reader", "password-123")["token"]

    async def failed(*args, **kwargs):
        raise error_type()

    portal.turn = failed
    with TestClient(create_app(portal), base_url="http://127.0.0.1") as client:
        response = client.post("/v1/saves/game/turns" + ("/stream" if stream else ""),
                               headers={"Authorization": f"Bearer {token}"},
                               json={"text": "private-prompt-sentinel", "request_id": "budget"})
        payload = ([json.loads(line[6:]) for line in response.text.splitlines()
                    if line.startswith("data: ")][-1] if stream else response.json())
    assert payload["code"] == code
    assert payload["retryable"] is False
    assert payload["commit_state"] == "unknown"
    assert payload["request_id"] == "budget"
    assert "private-" not in response.text


@pytest.mark.parametrize('stream', [False, True])
def test_budget_overflow_is_safe_nonretryable_and_preserves_world(portal, stream):
    from unittest.mock import patch
    from sqlalchemy import text
    from scenario_fixtures import synthetic_package
    player = portal.register('budget-reader', 'password-123')
    game_id = asyncio.run(portal.create_save(player['token'], 'npc-chat'))['game_id']
    factory = portal.gameplay.factory
    model = AsyncMock()
    factory.offline_models.model = model
    factory.invalidate(game_id)
    original = factory.engine
    marker = 'PRIVATE_REQUIRED_RULE'
    book = json.loads((ROOT / 'examples/freeform/worldbook.json').read_text(encoding='utf-8'))
    book['entries'].append({'id': 'required', 'kind': 'rule', 'visibility': 'public', 'text': marker * 20000})
    oversized = synthetic_package(worldbook=book)
    # Use the actual factory/projector with an oversized authored mandatory rule.
    def bounded(item, package, save, **kwargs):
        from dataclasses import replace
        return original(item, replace(package, worldbook=oversized.worldbook), save, **kwargs)

    def state():
        with portal.accounts.engine.connect() as db:
            return {table: tuple(db.execute(text(f'SELECT * FROM {table}')).fetchall())
                    for table in ('games', 'events', 'observations', 'agent_contexts',
                                  'turn_settlement_intents', 'portal_turns', 'credit_ledger')}
    before = state()
    with patch.object(factory, 'engine', side_effect=bounded), TestClient(create_app(portal), base_url='http://127.0.0.1') as client:
        response = client.post(f'/v1/saves/{game_id}/turns' + ('/stream' if stream else ''),
            headers={'Authorization': f"Bearer {player['token']}"}, json={'text': 'hello', 'request_id': 'budget'})
        payload = ([json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')][-1]
                   if stream else response.json())
    assert payload['retryable'] is False
    assert payload['commit_state'] == 'unknown'
    assert payload['code'] == 'CONTEXT_BUDGET_EXCEEDED'
    assert marker not in response.text and 'decisions' not in response.text
    assert state() == before
    model.assert_not_called()


@pytest.fixture
def portal(tmp_path, monkeypatch):
    monkeypatch.setenv("STORY_UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("STORY_MODEL_API_KEY", "offline-test")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "")
    instance = build_portal(ROOT / "examples/catalog.json", load_settings(ROOT / "config/local.json"),
                            str(tmp_path / "portal.sqlite3"),
                            prologue_generator=AsyncMock(return_value='A quiet opening.'),
                            runtime_factory_builder=OfflineRuntimeFactory)
    try:
        yield instance
    finally:
        instance.close()


@pytest.mark.parametrize("stream", [False, True])
def test_invalid_input_is_explicitly_not_started(portal, stream):
    token = portal.register("reader", "password-123")["token"]
    game_id = asyncio.run(portal.create_save(token, "npc-chat"))["game_id"]
    version = portal.gameplay.store.load(game_id).version
    with TestClient(create_app(portal), base_url="http://127.0.0.1") as client:
        response = client.post(f"/v1/saves/{game_id}/turns" + ("/stream" if stream else ""),
                               headers={"Authorization": f"Bearer {token}"},
                               json={"text": "/choose dockhand " + "x" * 10000,
                                     "request_id": "oversized"})
        payload = ([json.loads(line[6:]) for line in response.text.splitlines()
                    if line.startswith("data: ")][-1] if stream else response.json())
        assert payload.get("commit_state") == "not_started"
        assert payload["retryable"] is False
        assert payload["request_id"] == "oversized"
        assert portal.gameplay.store.load(game_id).version == version


@pytest.mark.parametrize("stream", [False, True])
def test_unclassified_value_error_does_not_authorize_new_input(portal, stream):
    token = portal.register("reader", "password-123")["token"]

    async def failed_after_commit(*args, **kwargs):
        raise ValueError("post-commit processing failed")

    portal.turn = failed_after_commit
    with TestClient(create_app(portal), base_url="http://127.0.0.1") as client:
        response = client.post("/v1/saves/game/turns" + ("/stream" if stream else ""),
                               headers={"Authorization": f"Bearer {token}"},
                               json={"text": "hello", "request_id": "uncertain"})
        payload = ([json.loads(line[6:]) for line in response.text.splitlines()
                    if line.startswith("data: ")][-1] if stream else response.json())
        assert payload.get("commit_state") == "unknown"
        assert payload["request_id"] == "uncertain"


def test_blocked_public_save_does_not_hide_other_saves_or_mask_authentication(portal):
    author = portal.register("author", "password-123")
    reader = portal.register("reader", "password-123")
    admin = portal.register("administrator", "password-123")
    portal.access.bootstrap_admin("administrator")
    draft = asyncio.run(portal.upload_scenario(author["token"], "Story", "", archive()))
    submission = portal.submit_scenario(author["token"], draft["id"])
    portal.review_decide(admin["token"], submission["submission_id"], "approved")
    blocked = asyncio.run(portal.create_save(reader["token"], draft["id"]))["game_id"]
    available = asyncio.run(portal.create_save(reader["token"], "npc-chat"))["game_id"]
    portal.admin_release_state(admin["token"], draft["id"], "blocked", "Test restriction")
    with TestClient(create_app(portal), base_url="http://127.0.0.1") as client:
        result = client.get("/v1/saves", headers={"Authorization": f"Bearer {reader['token']}"})
        assert result.status_code == 200
        saves = {item["game_id"]: item for item in result.json()["saves"]}
        assert saves[blocked]["available"] is False
        assert saves[blocked]["unavailable_reason"]
        assert saves[available]["available"] is True
        assert client.get("/v1/saves", headers={"Authorization": "Bearer expired"}).status_code == 401


def test_missing_uploaded_package_only_disables_its_save(portal):
    author = portal.register("author", "password-123")
    draft = asyncio.run(portal.upload_scenario(author["token"], "Story", "", archive()))
    portal.publish_scenario(author["token"], draft["id"])
    broken = asyncio.run(portal.create_save(author["token"], draft["id"]))["game_id"]
    normal = asyncio.run(portal.create_save(author["token"], "npc-chat"))["game_id"]
    package = portal.user_scenarios.package_store.materialize(f"{draft['id']}/{draft['version_id']}")
    (package / "manifest.json").unlink()
    with TestClient(create_app(portal), base_url="http://127.0.0.1") as client:
        response = client.get("/v1/saves", headers={"Authorization": f"Bearer {author['token']}"})
        assert response.status_code == 200
        saves = {item["game_id"]: item for item in response.json()["saves"]}
        assert saves[broken]["available"] is False
        assert saves[broken]["unavailable_reason"]
        assert saves[normal]["available"] is True


@pytest.mark.parametrize("stream", [False, True])
def test_missing_metering_requests_manual_recovery_without_unlocking_input(portal, stream):
    token = portal.register("reader", "password-123")["token"]
    game_id = asyncio.run(portal.create_save(token, "npc-chat"))["game_id"]
    commit_player_input(portal.gameplay.store, game_id, "portal-legacy:input", "hello")
    with TestClient(create_app(portal), base_url="http://127.0.0.1") as client:
        response = client.post(f"/v1/saves/{game_id}/turns" + ("/stream" if stream else ""),
                               headers={"Authorization": f"Bearer {token}"},
                               json={"text": "hello", "request_id": "legacy"})
        payload = ([json.loads(line[6:]) for line in response.text.splitlines()
                    if line.startswith("data: ")][-1] if stream else response.json())
        assert response.status_code == (200 if stream else 409)
        assert payload["code"] == "TURN_RECOVERY_REQUIRED"
        assert payload["commit_state"] == "unknown"
        assert payload["retryable"] is False
        assert payload["request_id"] == "legacy"
