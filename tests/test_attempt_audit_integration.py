import asyncio
import json
from dataclasses import replace

import pytest
from httpx2 import AsyncByteStream, AsyncClient as HttpAsyncClient, MockTransport, Response
from openai import AsyncClient as OpenAIAsyncClient

from storyloop_harness.generation import GenerationRequestLimitExceeded, GenerationTimeout
from storyloop_platform.config import ModelFactory, PlatformSettings, default_settings
from storyloop_platform.gameplay.attempt_audit_scope import attempt_audit_scope
from test_turn_failure_contract import portal
from test_generation_budget_integration import _completion
from test_scenario_lifecycle_concurrency import archive


def sdk_model(monkeypatch, responder, *, max_requests=None, timeout_seconds=None):
    transport = HttpAsyncClient(transport=MockTransport(responder))
    original_init = OpenAIAsyncClient.__init__

    def init(client, *args, **kwargs):
        kwargs["http_client"] = transport
        original_init(client, *args, **kwargs)

    monkeypatch.setattr(OpenAIAsyncClient, "__init__", init)
    settings = default_settings()
    if max_requests is not None or timeout_seconds is not None:
        raw = settings.model_dump(mode="json")
        if max_requests is not None:
            raw["models"]["story"]["generation_max_requests"] = max_requests
        if timeout_seconds is not None:
            raw["models"]["story"]["generation_timeout_seconds"] = timeout_seconds
        settings = PlatformSettings.model_validate(raw)
    model = ModelFactory(settings, env={"STORY_MODEL_API_KEY": "offline"}).create_model("single_turn")
    model.retry_delay = 0
    return model, transport


def sse_usage(prompt_tokens, completion_tokens):
    payload = {
        "id": "offline", "created": 0, "model": "qwen3.8-flash",
        "object": "chat.completion.chunk", "choices": [],
        "usage": {"prompt_tokens": prompt_tokens,
                  "completion_tokens": completion_tokens,
                  "total_tokens": prompt_tokens + completion_tokens},
    }
    return f"data: {json.dumps(payload)}\n\n".encode()


class BlockingUsageStream(AsyncByteStream):
    def __init__(self, prompt_tokens, completion_tokens):
        self.piece = sse_usage(prompt_tokens, completion_tokens)
        self.waiting = asyncio.Event()
        self.closed = False

    async def __aiter__(self):
        yield self.piece
        self.waiting.set()
        await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True


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


def test_portal_sdk_sse_cancel_after_report_persists_known_usage(portal, monkeypatch):
    stream = BlockingUsageStream(1200, 600)
    observed_errors = []

    def reply(request):
        return Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

    model, transport = sdk_model(monkeypatch, reply)
    model.stream = True
    portal.gameplay.factory.offline_models.model = model
    player = portal.register("sse-cancel-reader", "password-123")
    game = asyncio.run(portal.create_save(player["token"], "npc-chat"))["game_id"]

    async def turn():
        try:
            await portal.turn(player["token"], game, "Hello", "sse-cancel")
        except asyncio.CancelledError as error:
            observed_errors.append(error)
            raise

    async def run():
        task = asyncio.create_task(turn())
        try:
            await asyncio.wait_for(stream.waiting.wait(), timeout=10)
            task.cancel("original-cancellation")
            with pytest.raises(asyncio.CancelledError) as raised:
                await task
            return raised.value
        finally:
            await model.client.close()
            await transport.aclose()

    original = asyncio.run(run())
    assert type(original) is asyncio.CancelledError
    assert original is observed_errors[0]
    assert original.args == ("original-cancellation",)
    assert stream.closed
    row = portal.turns.audits.list_for_request(game, "sse-cancel")[0]
    assert row["outcome"] == "cancelled"
    assert row["error_type"] == "CancelledError"
    assert len(row["attempts"]) == 1
    assert row["attempts"][0]["outcome"] == "cancelled"
    assert row["attempts"][0]["usage_state"] == "reported"
    assert row["attempts"][0]["stream_complete"] is False
    assert row["summary"]["known_input_tokens"] == 1200
    assert row["summary"]["known_output_tokens"] == 600
    assert row["summary"]["known_cost_milli_points"] == 360
    assert row["summary"]["total_cost_milli_points"] is None


def test_portal_factory_deadline_keeps_earlier_reported_failure(portal, monkeypatch):
    sends = []

    async def reply(request):
        sends.append(request)
        if len(sends) == 1:
            return _completion("{}", prompt_tokens=1000, completion_tokens=500)
        await asyncio.Event().wait()

    model, transport = sdk_model(monkeypatch, reply, timeout_seconds=0.3)
    portal.gameplay.factory.offline_models.model = model
    player = portal.register("deadline-audit-reader", "password-123")
    game = asyncio.run(portal.create_save(player["token"], "npc-chat"))["game_id"]

    async def run():
        try:
            with pytest.raises(GenerationTimeout) as raised:
                await asyncio.wait_for(portal.turn(player["token"], game, "Hello", "deadline"),
                                       timeout=10)
            return raised.value
        finally:
            await model.client.close()
            await transport.aclose()

    original = asyncio.run(run())
    assert type(original) is GenerationTimeout
    assert len(sends) >= 2
    row = portal.turns.audits.list_for_request(game, "deadline")[0]
    assert row["outcome"] == "failed"
    assert row["error_type"] == "GenerationTimeout"
    assert row["attempts"][0]["outcome"] == "failed"
    assert row["attempts"][0]["usage_state"] == "reported"
    assert row["summary"]["known_input_tokens"] == 1000
    assert row["summary"]["known_output_tokens"] == 500
    assert row["summary"]["known_cost_milli_points"] == 300
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


def test_prepared_settlement_replay_has_no_new_audit_or_model_send(portal, monkeypatch):
    sends = []

    def reply(request):
        sends.append(request)
        return _completion('{"prose":"A quiet harbor."}', prompt_tokens=1000,
                           completion_tokens=500)

    model, transport = sdk_model(monkeypatch, reply)
    portal.gameplay.factory.offline_models.model = model
    player = portal.register("prepared-audit-reader", "password-123")
    game = asyncio.run(portal.create_save(player["token"], "npc-chat"))["game_id"]
    billing = portal.billing
    original_settle = billing.settle_turn

    def fail_settlement(*args, **kwargs):
        raise RuntimeError("settlement unavailable")

    monkeypatch.setattr(billing, "settle_turn", fail_settlement)
    with pytest.raises(RuntimeError, match="settlement unavailable"):
        asyncio.run(portal.turn(player["token"], game, "Hello", "prepared-audit"))
    before = portal.turns.audits.list_for_request(game, "prepared-audit")
    assert len(before) == 1 and before[0]["outcome"] == "succeeded"
    assert len(sends) == 1

    monkeypatch.setattr(billing, "settle_turn", original_settle)
    receipt = asyncio.run(portal.turn(player["token"], game, "Hello", "prepared-audit"))
    assert receipt["billing"]["input_tokens"] == 1000
    assert asyncio.run(portal.turn(player["token"], game, "Hello", "prepared-audit")) == receipt
    assert portal.turns.audits.list_for_request(game, "prepared-audit") == before
    assert len(sends) == 1
    asyncio.run(model.client.close())
    asyncio.run(transport.aclose())


def test_concurrent_players_capture_only_their_own_model_attempt(portal, monkeypatch):
    entered = 0
    both_entered = asyncio.Event()
    sends = []

    async def reply(request):
        nonlocal entered
        sends.append(request)
        entered += 1
        if entered == 2:
            both_entered.set()
        await asyncio.wait_for(both_entered.wait(), timeout=10)
        body = request.content.decode()
        tokens = 101 if "Alpha signal" in body else 202
        return _completion('{"prose":"A quiet harbor."}', prompt_tokens=tokens,
                           completion_tokens=50)

    model, transport = sdk_model(monkeypatch, reply)
    portal.gameplay.factory.offline_models.model = model
    alpha = portal.register("audit-alpha", "password-123")
    beta = portal.register("audit-beta", "password-123")
    game_a = asyncio.run(portal.create_save(alpha["token"], "npc-chat"))["game_id"]
    game_b = asyncio.run(portal.create_save(beta["token"], "npc-chat"))["game_id"]

    async def run():
        await asyncio.gather(
            portal.turn(alpha["token"], game_a, "Alpha signal", "concurrent-a"),
            portal.turn(beta["token"], game_b, "Beta signal", "concurrent-b"),
        )
        await model.client.close()
        await transport.aclose()

    asyncio.run(run())
    row_a = portal.turns.audits.list_for_request(game_a, "concurrent-a")
    row_b = portal.turns.audits.list_for_request(game_b, "concurrent-b")
    assert len(sends) == 2
    assert len(row_a) == len(row_b) == 1
    assert row_a[0]["player_id"] == alpha["player_id"]
    assert row_b[0]["player_id"] == beta["player_id"]
    assert row_a[0]["summary"]["known_input_tokens"] == 101
    assert row_b[0]["summary"]["known_input_tokens"] == 202
    assert len(row_a[0]["attempts"]) == len(row_b[0]["attempts"]) == 1


def test_preview_is_unbilled_but_records_usage_with_unknown_cost(portal, monkeypatch):
    sends = []

    def reply(request):
        sends.append(request)
        return _completion('{"prose":"A quiet harbor."}', prompt_tokens=500,
                           completion_tokens=200)

    model, transport = sdk_model(monkeypatch, reply)
    portal.gameplay.factory.offline_models.model = model
    author = portal.register("preview-audit-author", "password-123")
    reviewer = portal.register("preview-audit-reviewer", "password-123")
    portal.access.bootstrap_admin("preview-audit-reviewer")
    draft = asyncio.run(portal.upload_scenario(author["token"], "Preview", "", archive()))
    submission = portal.submit_scenario(author["token"], draft["id"])
    before_wallet = portal.wallet(reviewer["token"])
    game = asyncio.run(portal.create_review_preview(reviewer["token"],
                                                   submission["submission_id"]))["game_id"]
    response = asyncio.run(portal.turn(reviewer["token"], game, "Hello", "preview-audit"))
    rows = portal.turns.audits.list_for_request(game, "preview-audit")
    assert "billing" not in response
    assert portal.wallet(reviewer["token"]) == before_wallet
    assert portal.turns.settlements.get(game, "preview-audit", "Hello") is None
    assert len(rows) == 1 and rows[0]["outcome"] == "succeeded"
    assert rows[0]["policy"] is None
    assert rows[0]["summary"]["known_input_tokens"] == 500
    assert rows[0]["summary"]["known_cost_milli_points"] is None
    assert rows[0]["summary"]["total_cost_milli_points"] is None
    assert len(sends) == 1
    asyncio.run(model.client.close())
    asyncio.run(transport.aclose())


def test_unbilled_standard_turn_records_usage_without_estimated_cost(portal, monkeypatch):
    model, transport = sdk_model(
        monkeypatch,
        lambda request: _completion('{"prose":"A quiet harbor."}',
                                    prompt_tokens=700, completion_tokens=300),
    )
    portal.gameplay.factory.offline_models.model = model
    player = portal.register("unbilled-audit-reader", "password-123")
    game = asyncio.run(portal.create_save(player["token"], "npc-chat"))["game_id"]
    before_wallet = portal.wallet(player["token"])
    portal.turns.billing = None
    response = asyncio.run(portal.turn(player["token"], game, "Hello", "unbilled-audit"))
    row = portal.turns.audits.list_for_request(game, "unbilled-audit")[0]
    assert "billing" not in response
    assert portal.wallet(player["token"]) == before_wallet
    assert row["outcome"] == "succeeded"
    assert row["policy"] is None
    assert row["summary"]["known_input_tokens"] == 700
    assert row["summary"]["known_cost_milli_points"] is None
    assert row["summary"]["total_cost_milli_points"] is None
    asyncio.run(model.client.close())
    asyncio.run(transport.aclose())
