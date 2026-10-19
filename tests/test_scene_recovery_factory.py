"""Captured player scene recovery runtime contracts."""

import json
from dataclasses import FrozenInstanceError, replace
from unittest.mock import Mock

import pytest

from storyloop_harness import ScenarioPackage
from storyloop_harness.advanced import GeneratedSceneRecovery
from storyloop_platform.config import ModelFactory, PlatformSettings
from storyloop_platform.gameplay.factory import RuntimeFactory
from storyloop_platform.gameplay.scene_recovery import SceneRecoveryRuntime
from test_turn_failure_contract import portal


def native(portal, env=None):
    old = portal.gameplay.factory
    models = ModelFactory(old.settings, env={"STORY_MODEL_API_KEY": "dummy"} if env is None else env)
    return RuntimeFactory(settings=old.settings, store=old.store, accounts=old.accounts,
                          generation_settings=old.generation_settings_store,
                          models=models, telemetry=old.telemetry)


def fixture_parts(portal):
    item = portal.gameplay.game_access.listing_for("unused", "npc-chat")
    package = ScenarioPackage.load(item.package_path)
    package.seed_game(portal.gameplay.store, "recovery-factory")
    return item, package, "recovery-factory"


def changed_settings(factory, change):
    raw = factory.settings.model_dump()
    change(raw)
    factory.settings = PlatformSettings.model_validate(raw)
    factory.models.settings = factory.settings


def test_native_plan_is_frozen_and_capability_is_explicit(portal):
    item, package, game_id = fixture_parts(portal)
    assert portal.gameplay.factory.scene_recovery_plan(item, package, game_id) is None
    factory = native(portal)
    assert isinstance(factory, SceneRecoveryRuntime)
    plan = factory.scene_recovery_plan(item, package, game_id)
    assert plan is not None
    with pytest.raises(FrozenInstanceError):
        plan.generation_identity = "altered"
    assert len(plan.generation_identity) == 64
    assert (plan.execution_max_requests, plan.execution_timeout_seconds) == (6, 180)


@pytest.mark.parametrize("change", [
    lambda raw: raw["models"][raw["routes"]["single_turn"]].__setitem__("model", "next-model"),
    lambda raw: raw["models"][raw["routes"]["single_turn"]]["generation"].__setitem__("top_p", 0.7),
    lambda raw: raw["models"][raw["routes"]["single_turn"]].__setitem__("generation_max_requests", 2),
    lambda raw: raw["models"][raw["routes"]["single_turn"]].__setitem__("generation_timeout_seconds", 45),
    lambda raw: raw["models"][raw["routes"]["single_turn"]].__setitem__("timeout_seconds", 60),
    lambda raw: raw["models"][raw["routes"]["single_turn"]].__setitem__("max_retries", 1),
    lambda raw: raw["models"][raw["routes"]["single_turn"]]["extra_body"].__setitem__("enable_thinking", True),
    lambda raw: raw["runtime"].__setitem__("max_steps", 4),
    lambda raw: raw["runtime"].__setitem__("max_npc_replies", 2),
    lambda raw: raw["runtime"].__setitem__("turn_max_requests", 5),
    lambda raw: raw["runtime"].__setitem__("turn_timeout_seconds", 120),
])
def test_identity_changes_with_effective_runtime_contract(portal, change):
    item, package, game_id = fixture_parts(portal)
    factory = native(portal)
    before = factory.scene_recovery_plan(item, package, game_id)
    changed_settings(factory, change)
    assert factory.scene_recovery_plan(item, package, game_id).generation_identity != before.generation_identity


def test_identity_excludes_prices_and_credentials_but_tracks_endpoint(portal):
    item, package, game_id = fixture_parts(portal)
    env = {"STORY_MODEL_API_KEY": "one"}
    factory = native(portal, env)
    first = factory.scene_recovery_plan(item, package, game_id)
    env["STORY_MODEL_API_KEY"] = "two"
    assert factory.scene_recovery_plan(item, package, game_id).generation_identity == first.generation_identity
    raw = factory.settings.model_dump()
    profile = raw["models"][raw["routes"]["single_turn"]]
    if profile.get("rate") is not None:
        profile["rate"]["pricing_version"] = "new-price"
    factory.settings = PlatformSettings.model_validate(raw)
    factory.models.settings = factory.settings
    second = factory.scene_recovery_plan(item, package, game_id)
    assert (second.base_url, second.context_budget, second.generation_settings, second.max_steps) == (first.base_url, first.context_budget, first.generation_settings, first.max_steps)
    before_model = json.loads(first.model_config_json)
    after_model = json.loads(second.model_config_json)
    before_model.pop('rate', None)
    after_model.pop('rate', None)
    assert before_model == after_model
    assert second.generation_identity == first.generation_identity
    raw = factory.settings.model_dump()
    provider = factory.settings.model_for("single_turn").provider
    raw["providers"][provider]["api_key_env"] = "ROTATED_SECRET_ENV"
    factory.settings = PlatformSettings.model_validate(raw)
    factory.models.settings = factory.settings
    assert factory.scene_recovery_plan(item, package, game_id).generation_identity == first.generation_identity
    endpoint_var = factory.settings.providers[provider].base_url_env
    if endpoint_var:
        env[endpoint_var] = "https://other.example/v1"
    else:
        raw = factory.settings.model_dump()
        raw["providers"][provider]["base_url"] = "https://other.example/v1"
        factory.settings = PlatformSettings.model_validate(raw)
        factory.models.settings = factory.settings
    assert factory.scene_recovery_plan(item, package, game_id).generation_identity != first.generation_identity


def test_recovery_only_never_constructs_model_and_does_not_reuse_warm_normal_engine(portal):
    item, package, game_id = fixture_parts(portal)
    factory = native(portal)
    plan = factory.scene_recovery_plan(item, package, game_id)
    normal = factory.scene_recovery_engine(item, package, game_id, plan=plan, recovery_only=False)
    factory.require_credentials = Mock(side_effect=AssertionError("credential check"))
    factory.models.api_key = Mock(side_effect=AssertionError("secret read"))
    factory.models.create_model = Mock(side_effect=AssertionError("client construction"))
    factory.models.create_resolved_model = Mock(side_effect=AssertionError("client construction"))
    recovery = factory.scene_recovery_engine(item, package, game_id, plan=plan, recovery_only=True)
    assert recovery is not normal
    assert recovery.generated_scene_recovery == GeneratedSceneRecovery(
        factory.store, plan.generation_identity, protect_uncertain_generation=True)
    assert normal.generated_scene_recovery.protect_uncertain_generation is True
    assert recovery.session.projector.context_budget == plan.context_budget
    assert factory.scene_recovery_engine(item, package, game_id, plan=plan, recovery_only=True) is recovery
    factory.models.api_key.assert_not_called()


def test_normal_engine_uses_captured_endpoint_after_environment_changes(portal):
    item, package, game_id = fixture_parts(portal)
    factory = native(portal)
    provider = factory.settings.model_for("single_turn").provider
    endpoint_var = factory.settings.providers[provider].base_url_env
    if not endpoint_var:
        pytest.skip("default provider has no endpoint environment override")
    env = factory.models._env
    env[endpoint_var] = "https://first.example/v1"
    plan = factory.scene_recovery_plan(item, package, game_id)
    env[endpoint_var] = "https://second.example/v1"
    normal = factory.scene_recovery_engine(item, package, game_id, plan=plan, recovery_only=False)
    assert str(normal.session.generator.model.client.base_url) == "https://first.example/v1/"
    assert factory.scene_recovery_plan(item, package, game_id).generation_identity != plan.generation_identity


def test_unsupported_modes_and_offline_subclass_keep_legacy_behavior(portal):
    item, package, game_id = fixture_parts(portal)
    factory = native(portal)
    assert factory.scene_recovery_plan(replace(item, mode="campaign"), package, game_id) is None
    assert factory.scene_recovery_plan(item, replace(package, presentation_mode="novel"), game_id) is None
    assert factory.scene_recovery_plan(item, replace(package, story_blueprint=Mock()), game_id) is None
    assert portal.gameplay.factory.scene_recovery_plan(item, package, game_id) is None


def test_clock_context_and_contract_versions_change_identity(portal, monkeypatch):
    from storyloop_platform.gameplay import factory as factory_module
    item, package, game_id = fixture_parts(portal)
    factory = native(portal)
    original = factory.scene_recovery_plan(item, package, game_id)
    assert factory.scene_recovery_plan(replace(item, turns_per_story_tick=2), package, game_id).generation_identity != original.generation_identity
    factory.generation_settings_store.put(
        game_id, replace(original.generation_settings, context_window_tokens=16000),
        factory.default_generation_settings())
    assert factory.scene_recovery_plan(item, package, game_id).generation_identity != original.generation_identity
    factory.generation_settings_store.put(game_id, original.generation_settings,
                                          factory.default_generation_settings())
    monkeypatch.setattr(factory_module, "PLATFORM_SCENE_CONTRACT", "platform-ordinary-scene-v1")
    assert factory.scene_recovery_plan(item, package, game_id).generation_identity != original.generation_identity


def test_no_credentials_required_for_plan_or_recovery_engine(portal):
    item, package, game_id = fixture_parts(portal)
    factory = native(portal, env={})
    plan = factory.scene_recovery_plan(item, package, game_id)
    engine = factory.scene_recovery_engine(item, package, game_id, plan=plan, recovery_only=True)
    assert engine.generated_scene_recovery.generation_identity == plan.generation_identity


def test_exact_factory_with_offline_models_is_not_native(portal):
    item, package, game_id = fixture_parts(portal)
    old = portal.gameplay.factory
    factory = RuntimeFactory(settings=old.settings, store=old.store, accounts=old.accounts,
                             generation_settings=old.generation_settings_store,
                             models=old.offline_models, telemetry=old.telemetry)
    assert factory.scene_recovery_plan(item, package, game_id) is None
