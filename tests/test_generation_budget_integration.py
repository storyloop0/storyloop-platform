"""A factory model exercises the real SDK request path with an offline transport."""

import asyncio

import pytest
from httpx2 import AsyncClient as HttpAsyncClient, MockTransport, Response
from openai import AsyncClient as OpenAIAsyncClient, InternalServerError
from pydantic import BaseModel

from storyloop_harness.generation import GenerationRequestLimitExceeded, GenerationTimeout
from storyloop_platform.config import ModelFactory, PlatformSettings, default_settings


class Decision(BaseModel):
    prose: str


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
