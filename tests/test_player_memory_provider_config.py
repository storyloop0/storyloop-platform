import json
import os

import httpx
import pytest

from storyloop_platform.config import ModelFactory, PlatformSettings, default_settings, load_settings
from storyloop_platform.config.schema import PlayerMemorySettings
from storyloop_platform.memory.providers import Mem0PlayerMemory


def test_generated_preference_age_config_defaults_and_explicit_off():
    assert PlayerMemorySettings().generated_preference_max_age_days == 90
    assert PlayerMemorySettings.model_validate({"generated_preference_max_age_days": None}).generated_preference_max_age_days is None
    for invalid in (0, -1, 1.5, "90"):
        with pytest.raises(ValueError):
            PlayerMemorySettings.model_validate({"generated_preference_max_age_days": invalid})
    assert default_settings("online").player_memory.generated_preference_max_age_days == 90
    assert load_settings("config/online.json").player_memory.generated_preference_max_age_days == 90


def test_mem0_clients_use_profile_providers_despite_global_openrouter_env(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-unrelated-router-key")
    monkeypatch.setenv("OPENROUTER_API_BASE", "https://unrelated-router.invalid/v1")
    raw = default_settings("online").model_dump(mode="json")
    raw["providers"].update(
        {
            "extraction": {
                "base_url": "https://chat-config.invalid/v1",
                "base_url_env": "EXTRACTION_BASE_URL",
                "api_key_env": "EXTRACTION_API_KEY",
            },
            "embedding": {
                "base_url": "https://embedding.invalid/v1",
                "api_key_env": "EMBEDDING_API_KEY",
            },
        }
    )
    raw["models"]["memory_extraction"]["provider"] = "extraction"
    raw["models"]["memory_embedding"]["provider"] = "embedding"
    factory = ModelFactory(
        PlatformSettings.model_validate(raw),
        env={
            "EXTRACTION_API_KEY": "synthetic-extraction-key",
            "EXTRACTION_BASE_URL": "https://extraction.invalid/v1",
            "EMBEDDING_API_KEY": "synthetic-embedding-key",
        },
    )
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append((str(request.url), request.headers["Authorization"], body))
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(
                200,
                json={
                    "id": "offline-chat",
                    "object": "chat.completion",
                    "created": 0,
                    "model": body["model"],
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": "offline"},
                        }
                    ],
                },
            )
        return httpx.Response(
            200,
            json={
                "object": "list",
                "model": body["model"],
                "data": [{"object": "embedding", "index": 0, "embedding": [0.1]}],
                "usage": {"prompt_tokens": 1, "total_tokens": 1},
            },
        )

    memory = Mem0PlayerMemory(factory, tmp_path / "memory")
    try:
        llm, embedding = memory._memory.llm, memory._memory.embedding_model
        assert str(llm.client.base_url) == "https://extraction.invalid/v1/"
        assert llm.client.api_key == "synthetic-extraction-key"
        assert str(embedding.client.base_url) == "https://embedding.invalid/v1/"
        assert embedding.client.api_key == "synthetic-embedding-key"
        for model in (llm, embedding):
            assert model.client.timeout.read == 60
            assert model.client.timeout.connect == 10
            assert model.client.max_retries == 1
        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            llm.client = llm.client.with_options(http_client=client)
            embedding.client = embedding.client.with_options(http_client=client)
            assert (
                llm.generate_response([{"role": "user", "content": "offline"}])
                == "offline"
            )
            assert embedding.embed("offline") == [0.1]
        assert requests[0][:2] == (
            "https://extraction.invalid/v1/chat/completions",
            "Bearer synthetic-extraction-key",
        )
        assert requests[0][2]["model"] == raw["models"]["memory_extraction"]["model"]
        assert "models" not in requests[0][2]
        assert "route" not in requests[0][2]
        assert requests[1][:2] == (
            "https://embedding.invalid/v1/embeddings",
            "Bearer synthetic-embedding-key",
        )
        assert requests[1][2]["model"] == raw["models"]["memory_embedding"]["model"]
        assert requests[1][2]["dimensions"] == 1024
    finally:
        memory.close()
    assert os.environ["OPENROUTER_API_KEY"] == "synthetic-unrelated-router-key"
