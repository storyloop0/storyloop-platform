"""Actual runtime projection is bounded, detached and stable across SQL restart."""
import json
from unittest.mock import AsyncMock
from sqlalchemy import text
from storyloop_harness import TurnInput
from storyloop_harness.advanced import Observation, WorldEvent, estimate_tokens
from storyloop_platform.adapters.store import SQLiteGameStore
from storyloop_platform.portal.save_generation_settings import SaveGenerationSettings
from runtime_fakes import OfflineRuntimeFactory
from test_turn_failure_contract import portal
from test_runtime_factory import configure
from scenario_fixtures import source_package


def database_state(engine):
    with engine.connect() as db:
        return {table: tuple(db.execute(text(f'SELECT * FROM {table}')).fetchall())
                for table in ('games', 'events', 'observations', 'agent_contexts', 'save_generation_settings')}


def test_character_history_budget_and_visibility_survive_sql_restart(portal):
    factory = portal.gameplay.factory
    configure(factory, model_window=18000, runtime_window=20000, output=2048)
    factory.offline_models.model = AsyncMock()
    store = factory.store
    package = source_package()
    item = portal.gameplay.game_access.listing_for('unused', 'npc-chat')
    package.seed_game(store, 'save-a')
    package.seed_game(store, 'save-b')
    factory.generation_settings_store.put('save-a', SaveGenerationSettings(0.8, 24000), factory.default_generation_settings())
    for index in range(18):
        before = store.load('save-a')
        event_id = f'private-{index}'
        store.commit('save-a', before.version,
            WorldEvent(event_id, 'private_fact', None, None, before.tick, ()),
            (Observation(event_id + ':seen', event_id, 'guide', 'private_message',
                         f'Guide secret {index}: ' + 'quiet harbor recollection ' * 50, before.tick),), ())

    def project(runtime, game):
        return runtime.engine(item, package, game, overnight_requires_rest=True).project_context(
            TurnInput(game, 'Guide, what do you remember?', 'inspect', package.version))

    before = database_state(portal.accounts.engine)
    first = project(factory, 'save-a')
    report = first.report
    assert report.total_tokens == sum((report.system_tokens, report.material_tokens,
        report.schema_tokens, report.transport_tokens, report.output_tokens, report.safety_margin_tokens))
    assert report.total_tokens <= 18000
    assert report.system_tokens > 0 and report.schema_tokens > 0 and report.transport_tokens > 0
    assert report.estimator_id and report.policy_id
    assert estimate_tokens(json.dumps(first.request, ensure_ascii=False)) <= first.budget.max_material_tokens
    own = next(item for item in first.request['npc_contexts'] if item['id'] == 'guide')
    assert 'Guide secret 17' in str(own['own_history'])
    assert 'Guide secret' not in str(first.request['player_history'])
    assert all('Guide secret' not in str(item['own_history'])
               for item in first.request['npc_contexts'] if item['id'] != 'guide')
    assert 'Guide secret' not in json.dumps(report.to_dict())
    assert database_state(portal.accounts.engine) == before
    factory.offline_models.model.assert_not_called()
    reopened = SQLiteGameStore(str(portal.accounts.engine.url.database))
    runtime = OfflineRuntimeFactory(settings=factory.settings, store=reopened, accounts=portal.accounts,
        generation_settings=factory.generation_settings_store, models=factory.models, telemetry=factory.telemetry,
        model=AsyncMock())
    try:
        assert project(runtime, 'save-a') == first
        assert runtime.generation_settings('save-a').context_window_tokens == 18000
        assert 'Guide secret' not in str(project(runtime, 'save-b').request)
        first.request['player_action'] = 'mutated detached inspection'
        exported = report.to_dict()
        exported['decisions'] = ()
        assert project(runtime, 'save-a').request['player_action'] == 'Guide, what do you remember?'
        assert project(runtime, 'save-a').report == report
        assert database_state(portal.accounts.engine) == before
        runtime.offline_models.model.assert_not_called()
    finally:
        reopened.close()
