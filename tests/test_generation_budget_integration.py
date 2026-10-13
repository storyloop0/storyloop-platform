"""A factory model exercises the real SDK request path with an offline transport."""

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
from httpx2 import AsyncClient as HttpAsyncClient, MockTransport, Response
from openai import AsyncClient as OpenAIAsyncClient, InternalServerError
from pydantic import BaseModel

from storyloop_harness import ScenarioPackage, TurnEngine, TurnInput
from storyloop_harness.generation import GenerationRequestLimitExceeded, GenerationTimeout
from storyloop_harness.usage import ModelUsage, collect_attempts
from storyloop_platform.adapters.store import SQLiteGameStore
from storyloop_platform.config import ModelFactory, PlatformSettings, default_settings
from storyloop_platform.portal.billing import BillingPolicy


class Decision(BaseModel):
    prose: str


def _completion(arguments: str, *, prompt_tokens: int = 1000, completion_tokens: int = 500):
    return Response(200, json={
        "id": "chatcmpl-offline", "created": 0, "model": "qwen3.8-flash",
        "object": "chat.completion",
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                  "total_tokens": prompt_tokens + completion_tokens},
        "choices": [{"finish_reason": "tool_calls", "index": 0, "message": {
            "role": "assistant", "content": None, "tool_calls": [{"id": "decision-1",
                "type": "function", "function": {
                    "name": "generate_structured_output", "arguments": arguments}}]}}],
    })


def test_factory_model_through_engine_retains_failed_attempt_usage_observations(
    monkeypatch, tmp_path
):
    requests = []

    def invalid(request):
        requests.append(request)
        return _completion("{}")

    original_init = OpenAIAsyncClient.__init__
    transport = HttpAsyncClient(transport=MockTransport(invalid))

    def init(client, *args, **kwargs):
        kwargs["http_client"] = transport
        original_init(client, *args, **kwargs)

    monkeypatch.setattr(OpenAIAsyncClient, "__init__", init)
    raw = default_settings().model_dump(mode="json")
    raw["models"]["story"]["max_retries"] = 5
    raw["models"]["story"]["generation_max_requests"] = 2
    model = ModelFactory(PlatformSettings.model_validate(raw), env={
        "STORY_MODEL_API_KEY": "offline"
    }).create_model("single_turn")
    model.retry_delay = 0
    package = replace(ScenarioPackage.load(
        Path(__file__).resolve().parents[1] / "examples/freeform"
    ), initial_work=())
    store = SQLiteGameStore(str(tmp_path / "game.sqlite3"))
    package.seed_game(store, "game")
    before = store.load("game")

    async def run():
        try:
            with collect_attempts() as attempts:
                with pytest.raises(GenerationRequestLimitExceeded):
                    await asyncio.wait_for(TurnEngine(store, package, model).run_turn(
                        TurnInput("game", "hello", "budget-turn", package.version)
                    ), timeout=10)
            return attempts.records
        finally:
            await model.client.close()
            await transport.aclose()

    attempts = asyncio.run(run())
    assert len(requests) == 2
    reported = [attempt.usage for attempt in attempts if attempt.usage is not None]
    assert reported == [
        ModelUsage(model.model, "single_turn", 1000, 500)
    ] * 2
    # This is the price of observed provider usage, not a failed-turn settlement.
    policy = BillingPolicy.from_dict({
        "welcome_points": 500, "points_per_rmb": 50, "pricing_version": "offline",
        "models": {model.model: {"input_rmb_per_million": "1",
                                  "output_rmb_per_million": "2"}},
    }, {"single_turn": model.model})
    assert policy.price_milli_points(reported) == 200
    assert store.load("game") == before
    assert not store.event_exists("game", "budget-turn:input")


def test_factory_model_engine_fallback_bills_successful_response_as_before(
    monkeypatch, tmp_path
):
    requests = []

    def fallback(request):
        requests.append(request)
        if len(requests) == 1:
            return _completion("{}", prompt_tokens=1000, completion_tokens=500)
        return _completion('{"prose":"A quiet harbor."}',
                           prompt_tokens=1200, completion_tokens=600)

    original_init = OpenAIAsyncClient.__init__
    transport = HttpAsyncClient(transport=MockTransport(fallback))

    def init(client, *args, **kwargs):
        kwargs["http_client"] = transport
        original_init(client, *args, **kwargs)

    monkeypatch.setattr(OpenAIAsyncClient, "__init__", init)
    model = ModelFactory(default_settings(), env={
        "STORY_MODEL_API_KEY": "offline"
    }).create_model("single_turn")
    package = replace(ScenarioPackage.load(
        Path(__file__).resolve().parents[1] / "examples/freeform"
    ), initial_work=())
    store = SQLiteGameStore(str(tmp_path / "game.sqlite3"))
    package.seed_game(store, "game")

    async def run():
        try:
            with collect_attempts() as attempts:
                outcome = await asyncio.wait_for(TurnEngine(store, package, model).run_turn(
                    TurnInput("game", "hello", "fallback-turn", package.version)
                ), timeout=10)
            return outcome, attempts.records
        finally:
            await model.client.close()
            await transport.aclose()

    outcome, attempts = asyncio.run(run())
    assert len(requests) == 2
    assert [attempt.outcome for attempt in attempts] == ["failed", "succeeded"]
    assert [attempt.usage for attempt in attempts] == [
        ModelUsage(model.model, "single_turn", 1000, 500),
        ModelUsage(model.model, "single_turn", 1200, 600),
    ]
    assert outcome.model_usage == (ModelUsage(model.model, "single_turn", 1200, 600),)
    policy = BillingPolicy.from_dict({
        "welcome_points": 500, "points_per_rmb": 50, "pricing_version": "offline",
        "models": {model.model: {"input_rmb_per_million": "1",
                                  "output_rmb_per_million": "2"}},
    }, {"single_turn": model.model})
    assert policy.price_milli_points(list(outcome.model_usage)) == 120
    assert store.event_exists("game", "fallback-turn:input")


@pytest.mark.parametrize("max_retries,error_type", [
    (2, InternalServerError), (5, GenerationRequestLimitExceeded)
])
def test_factory_structured_503_has_one_budget_across_sdk_and_agentscope(
    monkeypatch, max_retries, error_type
):
    requests = []

    def fail(request):
        requests.append(request)
        return Response(503, headers={"retry-after-ms": "0"}, json={
            "error": {"message": "private-provider-sentinel", "type": "server_error"}
        })

    original_init = OpenAIAsyncClient.__init__
    transport = HttpAsyncClient(transport=MockTransport(fail))

    def init(client, *args, **kwargs):
        kwargs["http_client"] = transport
        original_init(client, *args, **kwargs)

    monkeypatch.setattr(OpenAIAsyncClient, "__init__", init)
    raw = default_settings().model_dump(mode="json")
    raw["models"]["story"]["max_retries"] = max_retries
    model = ModelFactory(PlatformSettings.model_validate(raw), env={
        "STORY_MODEL_API_KEY": "offline"
    }).create_model("single_turn")
    model.retry_delay = 0
    assert model.client.max_retries == max_retries

    async def run():
        try:
            with pytest.raises(error_type) as error:
                await asyncio.wait_for(model.generate_structured_output(
                    [{"role": "user", "content": "private-prompt-sentinel"}], Decision
                ), timeout=10)
            if isinstance(error.value, GenerationRequestLimitExceeded):
                assert "private-" not in str(error.value)
        finally:
            await model.client.close()
            await transport.aclose()

    asyncio.run(run())
    assert len(requests) == 3


def test_factory_model_concurrent_structured_calls_each_get_own_allowance(monkeypatch):
    requests = []

    async def fail(request):
        requests.append(request)
        await asyncio.sleep(0)
        return Response(503, headers={"retry-after-ms": "0"}, json={
            "error": {"message": "offline failure", "type": "server_error"}
        })

    original_init = OpenAIAsyncClient.__init__
    transport = HttpAsyncClient(transport=MockTransport(fail))

    def init(client, *args, **kwargs):
        kwargs["http_client"] = transport
        original_init(client, *args, **kwargs)

    monkeypatch.setattr(OpenAIAsyncClient, "__init__", init)
    raw = default_settings().model_dump(mode="json")
    raw["models"]["story"]["max_retries"] = 5
    model = ModelFactory(PlatformSettings.model_validate(raw), env={
        "STORY_MODEL_API_KEY": "offline"
    }).create_model("single_turn")
    model.retry_delay = 0

    async def invoke(label):
        with pytest.raises(GenerationRequestLimitExceeded):
            await model.generate_structured_output(
                [{"role": "user", "content": label}], Decision
            )

    async def run():
        try:
            await asyncio.wait_for(asyncio.gather(invoke("first"), invoke("second")), timeout=10)
        finally:
            await model.client.close()
            await transport.aclose()

    asyncio.run(run())
    assert len(requests) == 6
    assert model.client.max_retries == 5


def test_factory_model_deadline_raises_typed_timeout(monkeypatch):
    requests = []

    async def slow(request):
        requests.append(request)
        await asyncio.sleep(0.1)
        return Response(503, json={"error": {"message": "offline failure"}})

    original_init = OpenAIAsyncClient.__init__
    transport = HttpAsyncClient(transport=MockTransport(slow))

    def init(client, *args, **kwargs):
        kwargs["http_client"] = transport
        original_init(client, *args, **kwargs)

    monkeypatch.setattr(OpenAIAsyncClient, "__init__", init)
    raw = default_settings().model_dump(mode="json")
    raw["models"]["story"]["generation_timeout_seconds"] = 0.02
    model = ModelFactory(PlatformSettings.model_validate(raw), env={
        "STORY_MODEL_API_KEY": "offline"
    }).create_model("single_turn")

    async def run():
        try:
            with pytest.raises(GenerationTimeout):
                await asyncio.wait_for(model.generate_structured_output(
                    [{"role": "user", "content": "offline"}], Decision
                ), timeout=10)
        finally:
            await model.client.close()
            await transport.aclose()

    asyncio.run(run())
    assert len(requests) <= 1
