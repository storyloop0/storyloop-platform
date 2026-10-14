"""The Platform turn policy owns one execution scope and safe terminal errors."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import httpx
from fastapi.testclient import TestClient
from pydantic import ValidationError

from storyloop_harness.generation import (
    ExecutionBudget, ExecutionRequestLimitExceeded, ExecutionTimeout, Msg,
)
from storyloop_harness import ScenarioPackage
from storyloop_harness.advanced import SceneContext, Snapshot, StorySegment
from storyloop_harness.models.execution_control import current_execution_control
from storyloop_platform.config import PlatformSettings, default_settings
from storyloop_platform.generators.novel_narrator import NovelTurnNarrator
from storyloop_platform.generators.scene_narrator import CampaignSceneNarrator
from storyloop_platform.portal.http_api import create_app
from storyloop_platform.portal.turn_errors import turn_failure
from storyloop_platform.portal.turn_errors import TurnRecoveryRequired
from storyloop_platform.runtime.guidance import GuidanceAdvisor
from storyloop_platform.adapters.store import SQLiteGameStore
from test_turn_failure_contract import portal
from test_attempt_audit_integration import sdk_model
from test_generation_budget_integration import _completion, Decision


def test_runtime_defaults_and_overrides():
    raw = default_settings().model_dump(mode="json")
    raw["runtime"].pop("turn_max_requests", None)
    raw["runtime"].pop("turn_timeout_seconds", None)
    defaults = PlatformSettings.model_validate(raw)
    assert defaults.runtime.turn_max_requests == 6
    assert defaults.runtime.turn_timeout_seconds == 180
    raw["runtime"].update(turn_max_requests=4, turn_timeout_seconds=12.5)
    changed = PlatformSettings.model_validate(raw)
    assert changed.runtime.turn_max_requests == 4
    assert changed.runtime.turn_timeout_seconds == 12.5


@pytest.mark.parametrize("field,value", [
    ("turn_max_requests", True), ("turn_max_requests", 0),
    ("turn_timeout_seconds", 0), ("turn_timeout_seconds", float("inf")),
    ("turn_timeout_seconds", float("nan")),
])
def test_runtime_rejects_invalid_execution_budget(field, value):
    raw = default_settings().model_dump(mode="json")
    raw["runtime"][field] = value
    with pytest.raises(ValidationError):
        PlatformSettings.model_validate(raw)


def test_bootstrap_injects_execution_budget(portal):
    assert portal.turns.execution_budget == ExecutionBudget(6, 180)


@pytest.mark.parametrize("billing_mode", ["normal", "unbilled", "preview"])
def test_turn_scope_limits_actual_sdk_posts_before_world_commit(portal, monkeypatch, billing_mode):
    requests = []

    def invalid(request):
        requests.append(request)
        return _completion("{}")

    model, transport = sdk_model(monkeypatch, invalid)
    portal.gameplay.factory.offline_models.model = model
    portal.turns.execution_budget = ExecutionBudget(1, 180)
    player = portal.register("reader", "password-123")
    game = asyncio.run(portal.create_save(player["token"], "npc-chat"))["game_id"]
    if billing_mode == "unbilled":
        portal.turns.billing = None
    elif billing_mode == "preview":
        record = portal.accounts.get_save(portal.accounts.resolve_token(player["token"]), game)
        item = portal.turns.game_access.verified_listing(record)
        monkeypatch.setattr(portal.turns.game_access, "verified_listing", lambda *args: item)
        monkeypatch.setattr(portal.turns.moderation, "preview_for", lambda *args: object())
        monkeypatch.setattr(portal.turns.moderation, "preview_turn_count", lambda *args: 0)

    async def run():
        try:
            with pytest.raises(ExecutionRequestLimitExceeded):
                await portal.turn(player["token"], game, "Hello", "execution-limit")
        finally:
            await model.client.close()
            await transport.aclose()

    asyncio.run(run())
    assert len(requests) == 1
    assert not portal.gameplay.store.event_exists(game, "portal-execution-limit:input")


def test_one_turn_shares_six_actual_posts_across_engine_and_guidance(portal, monkeypatch):
    requests = []

    def reply(request):
        requests.append(request)
        # The scene generation uses its full local three-send allowance before
        # guidance starts, then guidance consumes the remaining three sends.
        return _completion("{}" if len(requests) <= 2 else '{"prose":"A quiet harbor."}')

    model, transport = sdk_model(monkeypatch, reply)
    portal.gameplay.factory.offline_models.model = model
    player = portal.register("reader", "password-123")
    game = asyncio.run(portal.create_save(player["token"], "npc-chat"))["game_id"]

    async def guidance(*args, **kwargs):
        for _ in range(4):
            await model([Msg("player", "offline guidance", "user")], structured_model=Decision)
        raise AssertionError("the seventh send must not return")

    monkeypatch.setattr(GuidanceAdvisor, "advise", guidance)

    async def run():
        try:
            with pytest.raises(ExecutionRequestLimitExceeded):
                await portal.turn(player["token"], game, "Hello", "six-posts")
        finally:
            await model.client.close()
            await transport.aclose()

    asyncio.run(run())
    assert len(requests) == 6
    assert portal.gameplay.store.event_exists(game, "portal-six-posts:input")
    assert portal.accounts.get_turn_response(game, "six-posts", "Hello") is None


@pytest.mark.parametrize("error_type", [ExecutionRequestLimitExceeded, ExecutionTimeout])
@pytest.mark.parametrize("component", ["guidance", "scene", "novel"])
def test_terminal_error_bypasses_quality_fallback(tmp_path, error_type, component):
    package = ScenarioPackage.load(Path(__file__).resolve().parents[1] / "examples/freeform")

    class FailingModel:
        async def __call__(self, *args, **kwargs):
            raise error_type()

        async def suggest(self, context):
            raise error_type()

    if component == "guidance":
        store = SQLiteGameStore(str(tmp_path / "game.sqlite3"))
        package.seed_game(store, "game")
        call = GuidanceAdvisor(store, package, provider=FailingModel()).advise(
            "game", store.load("game"))
    else:
        context = SceneContext("game", "hello", Snapshot("game", 0, 0, {}),
                               (StorySegment("scene", "hello"),), True, 1, "morning")
        presenter = (CampaignSceneNarrator(package, SimpleNamespace(steps=()), FailingModel())
                     if component == "scene" else NovelTurnNarrator(package, FailingModel()))
        call = presenter.present(context)
    with pytest.raises(error_type):
        asyncio.run(call)


@pytest.mark.parametrize("error_type", [ExecutionRequestLimitExceeded, ExecutionTimeout])
def test_campaign_message_writer_terminal_error_does_not_commit(error_type):
    from test_campaign import CampaignTests
    case = CampaignTests()
    case.setUp()

    class FailingWriter:
        async def write(self, *args, **kwargs):
            raise error_type()

    try:
        case.session.message_batch_writer = FailingWriter()
        asyncio.run(case.session.start("g"))
        asyncio.run(case.session.submit("g", "/choose m1", "choice"))
        asyncio.run(case.session.submit("g", "和甲聊天", "talk"))
        before = case.store.load("g")
        with pytest.raises(error_type):
            asyncio.run(case.session.submit("g", "/choose m1 我爱你", "message-terminal"))
        assert case.store.load("g") == before
        assert not case.store.event_exists("g", "message-terminal:campaign_choice")
    finally:
        case.tearDown()


def test_postcommit_timeout_is_audited_and_same_request_requires_recovery(portal, monkeypatch):
    portal.turns.execution_budget = ExecutionBudget(6, 0.2)
    player = portal.register("reader", "password-123")
    game = asyncio.run(portal.create_save(player["token"], "npc-chat"))["game_id"]
    finished_outside_scope = []
    original_finish = portal.turns.audits.finish

    def finish(*args, **kwargs):
        finished_outside_scope.append(current_execution_control() is None)
        return original_finish(*args, **kwargs)

    monkeypatch.setattr(portal.turns.audits, "finish", finish)

    async def progress(event):
        if event["type"] == "preview":
            await asyncio.sleep(1)

    with pytest.raises(ExecutionTimeout):
        asyncio.run(portal.turn(player["token"], game, "Hello", "timeout-after-commit",
                                progress=progress))
    assert portal.gameplay.store.event_exists(game, "portal-timeout-after-commit:input")
    with pytest.raises(TurnRecoveryRequired):
        asyncio.run(portal.turn(player["token"], game, "Hello", "timeout-after-commit"))
    assert finished_outside_scope == [True]
    rows = portal.turns.audits.list_for_request(game, "timeout-after-commit")
    assert rows[0]["outcome"] == "failed"
    assert portal.accounts.get_turn_response(game, "timeout-after-commit", "Hello") is None


def test_scope_starts_after_preferences_and_ends_after_view(portal, monkeypatch):
    from storyloop_platform.gameplay import turns as turn_module

    model, transport = sdk_model(monkeypatch, lambda request: _completion('{"prose":"A quiet harbor."}'))
    portal.gameplay.factory.offline_models.model = model
    player = portal.register("reader", "password-123")
    game = asyncio.run(portal.create_save(player["token"], "npc-chat"))["game_id"]
    seen = []
    original_preferences = portal.turns.memory_service.preferences_for
    original_view = turn_module.turn_view
    original_prepare = portal.turns.settlements.prepare

    async def preferences(*args, **kwargs):
        seen.append(("preferences", current_execution_control() is None))
        return await original_preferences(*args, **kwargs)

    def view(*args, **kwargs):
        seen.append(("view", current_execution_control() is not None))
        return original_view(*args, **kwargs)

    def prepare(*args, **kwargs):
        seen.append(("prepare", current_execution_control() is None))
        return original_prepare(*args, **kwargs)

    monkeypatch.setattr(portal.turns.memory_service, "preferences_for", preferences)
    monkeypatch.setattr(turn_module, "turn_view", view)
    monkeypatch.setattr(portal.turns.settlements, "prepare", prepare)
    async def run():
        try:
            result = await portal.turn(player["token"], game, "Hello", "scope-order")
            assert await portal.turn(player["token"], game, "Hello", "scope-order") == result
        finally:
            await model.client.close()
            await transport.aclose()

    asyncio.run(run())
    assert seen == [("preferences", True), ("view", True), ("prepare", True)]


def test_disconnected_stream_operation_still_reaches_execution_deadline(portal):
    from storyloop_harness.testing import OfflineModel

    entered = asyncio.Event()

    class StalledModel(OfflineModel):
        async def __call__(self, *args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

    portal.turns.execution_budget = ExecutionBudget(6, 0.15)
    portal.gameplay.factory.offline_models.model = StalledModel()

    async def run():
        player = portal.register("reader", "password-123")
        game = (await portal.create_save(player["token"], "npc-chat"))["game_id"]
        app = create_app(portal)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                                     base_url="http://127.0.0.1") as client:
            request = asyncio.create_task(client.post(
                f"/v1/saves/{game}/turns/stream",
                headers={"Authorization": f"Bearer {player['token']}"},
                json={"text": "Hello", "request_id": "stream-disconnect"}))
            await asyncio.wait_for(entered.wait(), timeout=2)
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
            await asyncio.sleep(0.25)
            portal.operations.assert_drained()
        rows = portal.turns.audits.list_for_request(game, "stream-disconnect")
        assert rows[0]["outcome"] == "failed"
        assert rows[0]["error_type"] == "ExecutionTimeout"

    asyncio.run(run())


@pytest.mark.parametrize("error,code,message", [
    (ExecutionRequestLimitExceeded(), "EXECUTION_REQUEST_LIMIT_EXCEEDED", "Turn execution request limit exceeded"),
    (ExecutionTimeout(), "EXECUTION_TIMEOUT", "Turn execution timeout exceeded"),
])
def test_execution_failure_is_fixed_and_safe(error, code, message):
    assert turn_failure(error, "request-1", message="private-provider-sentinel") == {
        "code": code, "message": message, "retryable": False,
        "commit_state": "unknown", "request_id": "request-1",
    }


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("error_type,code", [
    (ExecutionRequestLimitExceeded, "EXECUTION_REQUEST_LIMIT_EXCEEDED"),
    (ExecutionTimeout, "EXECUTION_TIMEOUT"),
])
def test_execution_http_error_is_safe(portal, stream, error_type, code):
    token = portal.register("reader", "password-123")["token"]

    async def failed(*args, **kwargs):
        if stream:
            await kwargs["progress"]({"type": "preview", "body": "public preview", "segments": []})
        raise error_type()

    portal.turn = failed
    with TestClient(create_app(portal), base_url="http://127.0.0.1") as client:
        response = client.post("/v1/saves/game/turns" + ("/stream" if stream else ""),
                               headers={"Authorization": f"Bearer {token}"},
                               json={"text": "private-prompt-sentinel", "request_id": "budget"})
        payload = ([json.loads(line[6:]) for line in response.text.splitlines()
                    if line.startswith("data: ")][-1] if stream else response.json())
    assert response.status_code == (200 if stream else 503)
    assert payload["code"] == code
    assert payload["retryable"] is False
    assert payload["commit_state"] == "unknown"
    assert payload["request_id"] == "budget"
    assert "private-" not in response.text
    if stream:
        events = [json.loads(line[6:]) for line in response.text.splitlines()
                  if line.startswith("data: ")]
        assert [event["type"] for event in events] == ["stage", "preview", "error"]
