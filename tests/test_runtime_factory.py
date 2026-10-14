"""Runtime construction remains accessible through the public factory seam."""

import pytest

from storyloop_harness import ScenarioPackage
from storyloop_harness import TurnInput
from storyloop_platform.config import PlatformSettings
from storyloop_platform.portal.save_generation_settings import SaveGenerationSettings
from storyloop_harness.advanced import PendingWork
from test_turn_failure_contract import portal


def test_player_and_preview_engines_leave_generated_scene_recovery_disabled(portal):
    factory = portal.gameplay.factory
    item = portal.gameplay.game_access.listing_for('unused', 'npc-chat')
    package = ScenarioPackage.load(item.package_path)
    package.seed_game(factory.store, 'recovery-disabled')
    player_engine = factory.engine(item, package, 'recovery-disabled')
    assert player_engine.generated_scene_recovery is None

    import asyncio
    from test_scenario_lifecycle_concurrency import archive

    author = portal.register('snapshot-preview-author', 'password-123')
    reviewer = portal.register('snapshot-preview-reviewer', 'password-123')
    portal.access.bootstrap_admin('snapshot-preview-reviewer')
    draft = asyncio.run(portal.upload_scenario(author['token'], 'Preview', '', archive()))
    submission = portal.submit_scenario(author['token'], draft['id'])
    preview = asyncio.run(portal.create_review_preview(reviewer['token'], submission['submission_id']))
    reviewer_id = portal.accounts.resolve_token(reviewer['token'])
    saved = portal.accounts.get_save(reviewer_id, preview['game_id'])
    preview_item = portal.gameplay.game_access.verified_listing(saved)
    preview_package = ScenarioPackage.load(preview_item.package_path)
    preview_engine = factory.engine(preview_item, preview_package, preview['game_id'])
    assert preview_engine.generated_scene_recovery is None


def configure(factory, model_window=65536, runtime_window=65536, output=None):
    data = factory.settings.model_dump()
    profile = data['models'][data['routes']['single_turn']]
    profile['context_window_tokens'] = model_window
    profile['generation']['max_tokens'] = output
    data['runtime']['context_window_tokens'] = runtime_window
    factory.settings = PlatformSettings.model_validate(data)


@pytest.mark.parametrize('model_window,runtime_window,saved,expected', [
    (16000, 24000, 32000, 16000), (24000, 16000, 32000, 16000),
    (24000, 32000, 16000, 16000), (24000, 32000, None, 24000),
])
@pytest.mark.parametrize('output', [None, 2048])
def test_actual_factory_uses_effective_budget(portal, model_window, runtime_window, saved, expected, output):
    factory = portal.gameplay.factory
    configure(factory, model_window, runtime_window, output)
    from unittest.mock import AsyncMock
    factory.offline_models.model = AsyncMock()
    item = portal.gameplay.game_access.listing_for('unused', 'npc-chat')
    package = ScenarioPackage.load(item.package_path)
    package.seed_game(factory.store, 'budget')
    if saved is not None:
        factory.generation_settings_store.put('budget', SaveGenerationSettings(0.7, saved), factory.default_generation_settings())
    before = factory.store.load('budget')
    engine = factory.engine(item, package, 'budget')
    projection = engine.project_context(TurnInput('budget', 'Hello', 'inspect', package.version))
    assert projection.report.context_window_tokens == expected
    assert projection.report.output_tokens == (output or 4096)
    assert projection.report.total_tokens <= expected
    assert projection.report.system_tokens > 0 and projection.report.schema_tokens > 0
    assert factory.generation_settings('budget').context_window_tokens == expected
    assert factory.store.load('budget') == before
    factory.offline_models.model.assert_not_called()
    if saved is not None:
        assert factory.generation_settings_store.get('budget', factory.default_generation_settings()).context_window_tokens == saved


def test_persisted_invalid_budget_rejected_before_model_or_cache(portal):
    factory = portal.gameplay.factory
    configure(factory, output=9000)
    item = portal.gameplay.game_access.listing_for('unused', 'npc-chat')
    package = ScenarioPackage.load(item.package_path)
    package.seed_game(factory.store, 'invalid-budget')
    factory.generation_settings_store.put('invalid-budget', SaveGenerationSettings(1.0, 9256), factory.default_generation_settings())
    with pytest.raises(ValueError, match='9256.*9000|9000.*9256'):
        factory.engine(item, package, 'invalid-budget')
    assert not factory.offline_models.calls
    assert not factory._turn_engines


def test_output_and_program_identity_are_part_of_engine_cache(portal):
    from storyloop_platform.runtime.campaign import CampaignProgram
    factory = portal.gameplay.factory
    item = portal.gameplay.game_access.listing_for('unused', 'npc-chat')
    package = ScenarioPackage.load(item.package_path)
    package.seed_game(factory.store, 'identity')
    plain = factory.engine(item, package, 'identity', overnight_requires_rest=True)
    program = CampaignProgram.from_dict({'id': 'test', 'ticks_per_day': 8, 'final_tick': 8,
        'steps': [{'id': 'scene', 'kind': 'scene', 'at': 0, 'text': 'Harbor', 'action_goal': 'Find the guide'},
                  {'id': 'choice', 'kind': 'choice', 'at': 1, 'prompt': 'Choose', 'options': [{'id': 'yes', 'label': 'Yes'}]},
                  {'id': 'end', 'kind': 'finale', 'at': 8, 'choice_key': 'choice', 'threshold': 1,
                   'success_text': 'Success', 'other_text': 'Done'}]})
    planned = factory.engine(item, package, 'identity', overnight_requires_rest=True, program=program)
    assert planned is not plain
    configure(factory, output=2048)
    changed = factory.engine(item, package, 'identity', overnight_requires_rest=True, program=program)
    assert changed is not planned
    assert changed.project_context(TurnInput('identity', 'hello', 'inspect', package.version)).report.output_tokens == 2048


@pytest.mark.parametrize('output,window', [(None, 4352), (9000, 9256)])
def test_single_turn_config_reserve_validation_is_safe(portal, output, window):
    data = portal.gameplay.factory.settings.model_dump()
    profile = data['models'][data['routes']['single_turn']]
    profile['generation']['max_tokens'] = output
    profile['extra_body'] = {'private_marker': 'DO_NOT_DISCLOSE'}
    data['runtime']['context_window_tokens'] = window
    with pytest.raises(ValueError) as caught:
        PlatformSettings.model_validate(data)
    assert 'DO_NOT_DISCLOSE' not in str(caught.value)
    assert 'single_turn' in str(caught.value)


def test_save_update_rejects_invalid_reserve_before_mutation(portal):
    import asyncio
    from sqlalchemy import text
    factory = portal.gameplay.factory
    player = portal.register('budget-owner', 'password-123')
    portal.accounts.create_save(player['player_id'], 'sample', 'settings-budget', 'sample', '1', 'hash')
    factory.generation_settings_store.put('settings-budget', SaveGenerationSettings(0.8, 16000), factory.default_generation_settings())
    with portal.accounts.engine.begin() as db:
        db.execute(text("INSERT INTO agent_contexts (game_id,actor_id,through_version,summary) VALUES ('settings-budget','player',3,'synthetic checkpoint')"))
    configure(factory, output=9000)
    with pytest.raises(ValueError, match='generation.max_tokens'):
        asyncio.run(portal.gameplay.set_save_settings(player['player_id'], 'settings-budget', 1.0, 8192))
    assert factory.generation_settings('settings-budget') == SaveGenerationSettings(0.8, 16000)
    with portal.accounts.engine.connect() as db:
        assert db.execute(text("SELECT summary FROM agent_contexts WHERE game_id='settings-budget'")).scalar() == 'synthetic checkpoint'


def test_engine_cache_and_invalidation_are_save_scoped(portal):
    factory = portal.gameplay.factory
    item = portal.gameplay.game_access.listing_for("unused", "npc-chat")
    package = ScenarioPackage.load(item.package_path)
    for game_id in ("one", "two"):
        package.seed_game(portal.gameplay.store, game_id)
    first = factory.engine(item, package, "one")
    other = factory.engine(item, package, "two")
    assert factory.engine(item, package, "one") is first
    factory.invalidate("one")
    assert factory.engine(item, package, "one") is not first
    assert factory.engine(item, package, "two") is other
    assert factory.generation_settings("one") == factory.default_generation_settings()


def test_supported_save_guard_runs_before_engine_cache_replay(portal):
    item = portal.gameplay.game_access.listing_for("unused", "npc-chat")
    package = ScenarioPackage.load(item.package_path)
    store = portal.gameplay.store
    package.seed_game(store, "guard")
    portal.gameplay.factory.engine(item, package, "guard")
    snapshot = store.load("guard")
    store.commit("guard", snapshot.version, None, (),
                 (PendingWork("obsolete", "npc_reply", 100, 10, None, {}),))
    with pytest.raises(ValueError, match="Unsupported legacy save"):
        portal.gameplay.factory.engine(item, package, "guard")


def test_campaign_novel_presenter_reads_recent_narration_only(portal):
    item = portal.gameplay.game_access.listing_for("unused", "npc-chat")
    package = ScenarioPackage.load(item.package_path)
    for index in range(4):
        portal.accounts.store_turn_response("prose", str(index), "input", {"segments": [
            {"kind": "narration", "text": f" narration-{index}-a "},
            {"kind": "dialogue", "text": "excluded dialogue"},
            {"kind": "narration", "text": f"narration-{index}-b"},
        ]})
    presenter = portal.gameplay.factory.novel_presenter(package, "prose")
    assert presenter.recent_prose("prose") == ["narration-2-b", "narration-3-a", "narration-3-b"]
