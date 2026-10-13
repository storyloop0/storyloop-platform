"""Model checks exercise real production construction with an offline SDK boundary."""
import asyncio
import importlib
import json
import sys
from unittest.mock import AsyncMock

import pytest
from httpx2 import Request, Response
from openai import APIStatusError, AsyncClient
from openai.types.chat import ChatCompletion

from storyloop_platform.config import ModelFactory, default_settings, load_settings


@pytest.fixture
def check():
    return importlib.import_module("storyloop_platform.cli.model_check")


@pytest.fixture
def sdk(monkeypatch):
    calls = []
    real_init = AsyncClient.__init__
    def init(client, *args, **kwargs):
        real_init(client, *args, **kwargs)
        async def completion(**request):
            calls.append((str(client.base_url), request))
            return ChatCompletion.model_validate({
                "id": "offline", "created": 0, "model": request["model"],
                "object": "chat.completion", "choices": [{"index": 0, "finish_reason": "stop",
                                                          "message": {"role": "assistant", "content": "OK"}}],
            })
        client.chat.completions.create = AsyncMock(side_effect=completion)
    monkeypatch.setattr(AsyncClient, "__init__", init)
    return calls


def test_check_uses_each_routed_endpoint_model_and_bounded_tokens(check, sdk, tmp_path):
    data = default_settings().model_dump(mode="json")
    data["providers"]["other"] = {"base_url": "https://other.invalid/v1", "api_key_env": "OTHER_KEY"}
    data["models"]["other"] = {"kind": "chat", "provider": "other", "model": "other-model",
                               "generation": {"max_tokens": 2000}, "rate": None}
    data["routes"]["followup_actions"] = "other"
    data["credits"] = None
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(data))
    factory = ModelFactory(load_settings(path), env={"STORY_MODEL_API_KEY": "offline", "OTHER_KEY": "offline"})
    results = asyncio.run(check.check_models(factory, ("single_turn", "followup_actions")))
    assert [item["task"] for item in results] == ["single_turn", "followup_actions"]
    assert all(item["ok"] for item in results)
    assert sdk[1][0] == "https://other.invalid/v1/"
    assert sdk[1][1]["model"] == "other-model"
    for _, request in sdk:
        assert 0 < request["max_completion_tokens"] <= 64
        assert "max_tokens" not in request
        assert request["stream"] is False


def test_unknown_task_validates_entire_selection_before_any_request(check, sdk):
    factory = ModelFactory(default_settings(), env={"STORY_MODEL_API_KEY": "offline"})
    with pytest.raises(ValueError):
        asyncio.run(check.check_models(factory, ("single_turn", "typo")))
    assert sdk == []


def test_cli_safe_failure_status_and_nonzero_exit(check, monkeypatch, capsys, tmp_path):
    real_init = AsyncClient.__init__
    def init(client, *args, **kwargs):
        real_init(client, *args, **kwargs)
        error = APIStatusError("private-key private-response", response=Response(
            429, request=Request("POST", "https://private.invalid/v1")), body={"secret": "private-body"})
        client.chat.completions.create = AsyncMock(side_effect=error)
    monkeypatch.setattr(AsyncClient, "__init__", init)
    monkeypatch.setenv("STORY_MODEL_API_KEY", "private-key")
    path = tmp_path / "settings.json"
    data = default_settings().model_dump(mode="json")
    for profile in data["models"].values():
        profile["max_retries"] = 1
    path.write_text(json.dumps(data))
    monkeypatch.setattr(sys, "argv", ["model_check", "--config", str(path), "--task", "single_turn"])
    assert check.main() == 1
    captured = capsys.readouterr()
    assert "private-" not in captured.out + captured.err
    result = json.loads(captured.out)[0]
    assert result["error_type"] == "APIStatusError"
    assert result["status_code"] == 429
    assert result["ok"] is False


def test_cli_typo_task_has_no_request_or_prompt(check, sdk, monkeypatch, capsys):
    monkeypatch.delenv("STORY_MODEL_API_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["model_check", "--task", "single_turn", "--task", "typo"])
    assert check.main() == 1
    assert not sdk
    assert json.loads(capsys.readouterr().out)[0]["ok"] is False


def test_cli_selected_task_does_not_require_unused_provider_or_memory_keys(check, sdk, monkeypatch, tmp_path):
    data = default_settings("online").model_dump(mode="json")
    data["providers"]["unused"] = {"base_url": "https://unused.invalid/v1", "api_key_env": "UNUSED_KEY"}
    data["models"]["memory_embedding"]["provider"] = "unused"
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(data))
    monkeypatch.setenv("STORY_MODEL_API_KEY", "offline")
    monkeypatch.delenv("UNUSED_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["model_check", "--config", str(path), "--profile", "online", "--task", "single_turn"])
    assert check.main() == 0
    assert len(sdk) == 1


@pytest.mark.parametrize("request_fails", [False, True], ids=["close-only", "request-and-close"])
def test_cli_close_failure_keeps_task_outcome_and_continues(check, sdk, monkeypatch, capsys, tmp_path,
                                                          request_fails):
    sdk_init = AsyncClient.__init__
    real_close = AsyncClient.close
    clients = []

    def init(client, *args, **kwargs):
        sdk_init(client, *args, **kwargs)
        clients.append(client)
        if len(clients) == 1:
            if request_fails:
                error = APIStatusError("private-request", response=Response(
                    429, request=Request("POST", "https://private.invalid/v1")), body=None)
                client.chat.completions.create = AsyncMock(side_effect=error)

            async def close():
                await real_close(client)
                raise RuntimeError("private-cleanup")

            client.close = close

    monkeypatch.setattr(AsyncClient, "__init__", init)
    monkeypatch.setenv("STORY_MODEL_API_KEY", "offline")
    data = default_settings().model_dump(mode="json")
    for profile in data["models"].values():
        profile["max_retries"] = 1
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(data))
    monkeypatch.setattr(sys, "argv", ["model_check", "--config", str(path),
                                     "--task", "single_turn", "--task", "followup_actions"])

    assert check.main() == 1
    captured = capsys.readouterr()
    assert "private-" not in captured.out + captured.err
    results = json.loads(captured.out)
    assert results[0] == {
        "task": "single_turn", "model": data["models"][data["routes"]["single_turn"]]["model"],
        "ok": False, "error_type": "APIStatusError" if request_fails else "RuntimeError",
        "status_code": 429 if request_fails else None,
    }
    assert results[1] == {
        "task": "followup_actions", "model": data["models"][data["routes"]["followup_actions"]]["model"],
        "ok": True,
    }
    assert [client.max_retries for client in clients] == [1, 0, 1, 0]
    assert len(sdk) == (1 if request_fails else 2)
