import asyncio
from dataclasses import replace

import pytest
from httpx2 import AsyncClient as HttpAsyncClient, MockTransport
from openai import AsyncClient as OpenAIAsyncClient

from storyloop_harness.generation import GenerationRequestLimitExceeded
from storyloop_platform.config import ModelFactory, PlatformSettings, default_settings
from storyloop_platform.gameplay.attempt_audit_scope import attempt_audit_scope
from test_turn_failure_contract import portal
from test_generation_budget_integration import _completion


def sdk_model(monkeypatch, responder, *, max_requests=None):
    transport = HttpAsyncClient(transport=MockTransport(responder))
    original_init = OpenAIAsyncClient.__init__

    def init(client, *args, **kwargs):
        kwargs["http_client"] = transport
        original_init(client, *args, **kwargs)

    monkeypatch.setattr(OpenAIAsyncClient, "__init__", init)
    settings = default_settings()
    if max_requests is not None:
        raw = settings.model_dump(mode="json")
        raw["models"]["story"]["generation_max_requests"] = max_requests
        settings = PlatformSettings.model_validate(raw)
    model = ModelFactory(settings, env={"STORY_MODEL_API_KEY": "offline"}).create_model("single_turn")
    model.retry_delay = 0
    return model, transport


def test_portal_turn_writes_model_execution_before_settlement_and_cache_replay(portal, monkeypatch):
    responses = []

    def reply(request):
        responses.append(request)
        return _completion('{"prose":"A quiet harbor."}', prompt_tokens=1200,
                           completion_tokens=600)

    model, transport = sdk_model(monkeypatch, reply)
    portal.gameplay.factory.offline_models.model = model
    player = portal.register("reader", "password-123")
    game = asyncio.run(portal.create_save(player["token"], "npc-chat"))["game_id"]
    result = asyncio.run(portal.turn(player["token"], game, "Hello", "audit-success"))
    rows = portal.turns.audits.list_for_request(game, "audit-success")
    assert len(rows) == 1
    assert rows[0]["outcome"] == "succeeded"
    assert rows[0]["summary"]["known_cost_milli_points"] is not None
    assert len(responses) == 1
    assert asyncio.run(portal.turn(player["token"], game, "Hello", "audit-success")) == result
    assert portal.turns.audits.list_for_request(game, "audit-success") == rows
    asyncio.run(model.client.close())
    asyncio.run(transport.aclose())


def test_fallback_audits_failed_and_success_usage_but_receipt_success_only(portal, monkeypatch):
    requests = []

    def fallback(request):
        requests.append(request)
        return (_completion("{}", prompt_tokens=1000, completion_tokens=500)
                if len(requests) == 1 else
                _completion('{"prose":"A quiet harbor."}', prompt_tokens=1200,
                            completion_tokens=600))

    model, transport = sdk_model(monkeypatch, fallback)
    portal.gameplay.factory.offline_models.model = model
    player = portal.register("fallback-reader", "password-123")
    game = asyncio.run(portal.create_save(player["token"], "npc-chat"))["game_id"]
    receipt = asyncio.run(portal.turn(player["token"], game, "Hello", "fallback"))
    audit = portal.turns.audits.list_for_request(game, "fallback")[0]
    assert [attempt["outcome"] for attempt in audit["attempts"]] == ["failed", "succeeded"]
    assert audit["summary"]["known_input_tokens"] == 2200
    assert audit["summary"]["known_output_tokens"] == 1100
    assert receipt["billing"]["input_tokens"] == 1200
    assert receipt["billing"]["output_tokens"] == 600
    asyncio.run(model.client.close())
    asyncio.run(transport.aclose())


def test_failed_model_execution_retries_same_request_with_new_execution_id(portal, monkeypatch):
    requests = []

    def invalid(request):
        requests.append(request)
        return _completion("{}", prompt_tokens=1000, completion_tokens=500)

    model, transport = sdk_model(monkeypatch, invalid, max_requests=2)
    portal.gameplay.factory.offline_models.model = model
    player = portal.register("failure-reader", "password-123")
    game = asyncio.run(portal.create_save(player["token"], "npc-chat"))["game_id"]
    wallet = portal.wallet(player["token"])
    for _ in range(2):
        with pytest.raises(GenerationRequestLimitExceeded):
            asyncio.run(portal.turn(player["token"], game, "Hello", "failed"))
    audits = portal.turns.audits.list_for_request(game, "failed")
    assert len(audits) == 2
    assert len({row["execution_id"] for row in audits}) == 2
    assert all(row["outcome"] == "failed" for row in audits)
    assert all(row["summary"]["known_input_tokens"] == 2000 for row in audits)
    assert all(row["summary"]["known_cost_milli_points"] > 0 for row in audits)
    assert all(row["summary"]["total_cost_milli_points"] is None for row in audits)
    assert portal.wallet(player["token"]) == wallet
    assert len(requests) == 4
    asyncio.run(model.client.close())
    asyncio.run(transport.aclose())


def test_scope_preserves_original_error_when_finish_fails(portal, caplog):
    class Broken:
        def start(self, **kwargs):
            pass

        def finish(self, *args, **kwargs):
            raise RuntimeError("PRIVATE_PROVIDER_SENTINEL")

    error = ValueError("original")
    with pytest.raises(ValueError) as raised:
        with attempt_audit_scope(Broken(), player_id="p", game_id="g", request_id="r",
                                 player_text="secret", policy=None):
            raise error
    assert raised.value is error
    assert "PRIVATE_PROVIDER_SENTINEL" not in caplog.text
    assert "secret" not in caplog.text
    assert "RuntimeError" in caplog.text


def test_scope_start_failure_does_not_skip_model_work(caplog):
    class Broken:
        def start(self, **kwargs):
            raise RuntimeError("PRIVATE_START_SENTINEL")

        def finish(self, *args, **kwargs):
            pytest.fail("finish should not run after failed start")

    ran = []
    with attempt_audit_scope(Broken(), player_id="p", game_id="g", request_id="r",
                             player_text="secret", policy=None):
        ran.append(True)
    assert ran == [True]
    assert "PRIVATE_START_SENTINEL" not in caplog.text
    assert "secret" not in caplog.text


def test_cancelled_scope_persists_cancelled_outcome(portal):
    error = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError) as raised:
        with attempt_audit_scope(portal.turns.audits, player_id="p", game_id="g",
                                 request_id="cancelled", player_text="secret", policy=None):
            raise error
    assert raised.value is error
    row = portal.turns.audits.list_for_request("g", "cancelled")[0]
    assert row["outcome"] == "cancelled"
    assert row["error_type"] == "CancelledError"
    assert row["summary"]["known_cost_milli_points"] is None
    assert row["summary"]["total_cost_milli_points"] is None


def test_portal_cancellation_preserves_cancelled_error_and_unknown_cost(portal, monkeypatch):
    entered = asyncio.Event()

    async def wait_for_cancel(request):
        entered.set()
        await asyncio.sleep(60)

    model, transport = sdk_model(monkeypatch, wait_for_cancel)
    portal.gameplay.factory.offline_models.model = model
    player = portal.register("cancel-reader", "password-123")
    game = asyncio.run(portal.create_save(player["token"], "npc-chat"))["game_id"]

    async def run():
        task = asyncio.create_task(portal.turn(player["token"], game, "Hello", "cancel"))
        await asyncio.wait_for(entered.wait(), timeout=10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await model.client.close()
        await transport.aclose()

    asyncio.run(run())
    row = portal.turns.audits.list_for_request(game, "cancel")[0]
    assert row["outcome"] == "cancelled"
    assert row["summary"]["total_cost_milli_points"] is None


def test_audit_finish_write_fault_keeps_successful_receipt(portal, monkeypatch, caplog):
    repository = portal.turns.audits

    def broken_finish(*args, **kwargs):
        raise RuntimeError("PRIVATE_AUDIT_SENTINEL")

    monkeypatch.setattr(repository, "finish", broken_finish)
    model, transport = sdk_model(monkeypatch, lambda request: _completion('{"prose":"A quiet harbor."}'))
    portal.gameplay.factory.offline_models.model = model
    player = portal.register("audit-fault-reader", "password-123")
    game = asyncio.run(portal.create_save(player["token"], "npc-chat"))["game_id"]
    receipt = asyncio.run(portal.turn(player["token"], game, "Hello", "audit-fault"))
    assert receipt["billing"]["input_tokens"] > 0
    assert "PRIVATE_AUDIT_SENTINEL" not in caplog.text
    assert repository.list_for_request(game, "audit-fault")[0]["outcome"] == "running"
    asyncio.run(model.client.close())
    asyncio.run(transport.aclose())
