import json

import pytest

from storyloop_platform.config import (
    ModelFactory,
    PlatformSettings,
    default_settings,
    load_settings,
)
from storyloop_harness.generation import GenerationBudget


@pytest.mark.parametrize(
    "section,field,value",
    [
        ("runtime", "surprise", "secret-value"),
        ("runtime", "followup_timeout_seconds", float("inf")),
    ],
)
def test_invalid_nested_values_are_rejected_without_echoing_input(
    section, field, value
):
    raw = default_settings().model_dump(mode="json")
    raw[section][field] = value
    with pytest.raises(ValueError) as error:
        PlatformSettings.model_validate(raw)
    assert field in str(error.value)
    assert "secret-value" not in str(error.value)


@pytest.mark.parametrize("change", ["provider", "route", "kind", "generation", "rates"])
def test_invalid_model_relationships_fail_before_resources(change):
    raw = default_settings().model_dump(mode="json")
    if change == "provider":
        raw["models"]["story"]["provider"] = "missing"
    if change == "route":
        raw["routes"]["single_turn"] = "missing"
    if change == "kind":
        raw["routes"]["single_turn"] = "memory_embedding"
    if change == "generation":
        raw["models"]["story"]["generation"]["temperatur"] = 0.5
    if change == "rates":
        raw["models"]["copy"] = json.loads(json.dumps(raw["models"]["story"]))
        raw["models"]["copy"]["rate"]["input_rmb_per_million"] = "999"
    with pytest.raises(ValueError):
        PlatformSettings.model_validate(raw)


def test_override_maps_replace_and_relative_storage_resolves(tmp_path):
    raw = default_settings().model_dump(mode="json")
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "models": {"only": raw["models"]["story"]},
                "routes": {task: "only" for task in raw["routes"]},
                "storage": {"path": "db.sqlite3"},
            }
        )
    )
    settings = load_settings(path)
    assert set(settings.models) == {"only"}
    assert settings.storage.path == str(tmp_path / "db.sqlite3")


def test_factory_uses_per_task_provider_generation_and_redacts_credentials():
    raw = default_settings().model_dump(mode="json")
    raw["providers"]["other"] = {
        "base_url": "https://other.invalid/v1",
        "api_key_env": "OTHER_KEY",
    }
    raw["models"]["light"] = {
        **raw["models"]["story"],
        "provider": "other",
        "generation": {"temperature": 0.2, "max_tokens": 321},
        "timeout_seconds": 25,
    }
    raw["routes"]["followup_actions"] = "light"
    settings = PlatformSettings.model_validate(raw)
    factory = ModelFactory(settings, env={"OTHER_KEY": "credential-value"})
    model = factory.create_model("followup_actions")
    assert str(model.client.base_url) == "https://other.invalid/v1/"
    assert model.parameters.temperature == 0.2
    assert model.parameters.max_tokens == 321
    assert model.client.timeout.read == 25
    assert "credential-value" not in repr(factory)
    assert "credential-value" not in repr(settings)


@pytest.mark.parametrize("field,value", [
    ("generation_max_requests", 0),
    ("generation_max_requests", -1),
    ("generation_max_requests", True),
    ("generation_timeout_seconds", 0),
    ("generation_timeout_seconds", float("inf")),
    ("generation_timeout_seconds", float("nan")),
])
def test_invalid_generation_budget_settings_are_rejected(field, value):
    raw = default_settings().model_dump(mode="json")
    raw["models"]["story"][field] = value
    with pytest.raises(ValueError) as error:
        PlatformSettings.model_validate(raw)
    assert field in str(error.value)


def test_factory_uses_custom_generation_budget_without_changing_sdk_retry_setting():
    raw = default_settings().model_dump(mode="json")
    raw["models"]["story"].update(
        generation_max_requests=1, generation_timeout_seconds=0.5
    )
    model = ModelFactory(PlatformSettings.model_validate(raw), env={
        "STORY_MODEL_API_KEY": "offline"
    }).create_model("single_turn")
    assert model.generation_budget == GenerationBudget(1, 0.5)
    assert model.client.max_retries == 2


def test_memory_profiles_have_independent_credentials_and_endpoints():
    raw = default_settings("online").model_dump(mode="json")
    raw["providers"]["embedding"] = {
        "base_url": "https://embedding.invalid/v1",
        "api_key_env": "EMBED_KEY",
    }
    raw["models"]["memory_embedding"]["provider"] = "embedding"
    factory = ModelFactory(
        PlatformSettings.model_validate(raw),
        env={"STORY_MODEL_API_KEY": "chat-key", "EMBED_KEY": "embed-key"},
    )
    config = factory.memory_config()
    assert config["llm"]["config"]["api_key"] == "chat-key"
    assert config["embedder"]["config"]["api_key"] == "embed-key"
    assert (
        config["embedder"]["config"]["openai_base_url"]
        == "https://embedding.invalid/v1"
    )


def test_environment_endpoint_cannot_embed_credentials():
    factory = ModelFactory(
        default_settings(),
        env={"STORY_MODEL_BASE_URL": "https://name:secret-value@example.invalid/v1"},
    )
    with pytest.raises(ValueError) as error:
        factory.base_url("default")
    assert "secret-value" not in str(error.value)


def test_memory_extraction_rejects_parameters_unsupported_by_mem0():
    raw = default_settings("online").model_dump(mode="json")
    raw["models"]["memory_extraction"]["extra_body"] = {"enable_thinking": False}
    with pytest.raises(ValueError, match="player_memory.extraction_profile"):
        PlatformSettings.model_validate(raw)


def test_billing_snapshot_roundtrip_preserves_prices():
    from storyloop_platform.config import PlatformResources
    from storyloop_platform.portal.billing import BillingPolicy

    policy = PlatformResources(default_settings()).billing_policy
    restored = BillingPolicy.from_dict(policy.to_dict(), {})
    assert restored == policy


@pytest.mark.parametrize("environment", [None, {}, [], 1])
def test_loader_rejects_invalid_environment_values(tmp_path, environment):
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps({"environment": environment}))
    with pytest.raises(ValueError, match="environment"):
        load_settings(path)
