"""Public inspection matches actual runtime clock/program and SDK invocation."""
import asyncio
import json
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from openai.types.chat import ChatCompletion
from storyloop_harness import ScenarioPackage, TurnInput
from storyloop_harness.advanced import Effect, StoryClock, WorldEvent
from storyloop_harness.testing import OfflineModel
from storyloop_platform.config import ModelFactory
from scenario_fixtures import source_package
from test_turn_failure_contract import portal
from test_runtime_factory import configure


class CaptureModel:
    def __init__(self):
        self.requests = []
        self.kwargs = []

    async def __call__(self, messages, **kwargs):
        content = messages[-1]['content']
        if isinstance(content, list):
            content = ''.join(part['text'] for part in content if part.get('type') == 'text')
        self.requests.append(json.loads(content))
        self.kwargs.append(kwargs)
        return await OfflineModel()(messages, **kwargs)


def test_source_inspection_uses_scaled_clock_and_matches_generation(portal):
    factory = portal.gameplay.factory
    package = source_package()
    item = replace(portal.gameplay.game_access.listing_for('unused', 'npc-chat'), turns_per_story_tick=3)
    package.seed_game(factory.store, 'source-clock')
    before = factory.store.load('source-clock')
    factory.store.commit('source-clock', before.version, WorldEvent('clock-fixture', 'fixture', None, None, 13, ()), (), ())
    capture = CaptureModel()
    factory.offline_models.model = capture
    engine = factory.engine(item, package, 'source-clock', overnight_requires_rest=True)
    turn = TurnInput('source-clock', 'Dockhand, hello', 'clock-turn', package.version)
    projection = engine.project_context(turn)
    assert projection.request['day'] == 13 // (package.ticks_per_day * 3) + 1
    assert projection.request['time_period'] == StoryClock(package.ticks_per_day * 3).period(13)
    assert not capture.requests
    asyncio.run(engine.run_turn(turn))
    assert capture.requests[0] == projection.request
    assert capture.kwargs[0]['max_completion_tokens'] == projection.report.output_tokens
    assert 'max_tokens' not in capture.kwargs[0]
    before = factory.store.load('source-clock')
    with pytest.raises(ValueError, match='version'):
        engine.project_context(replace(turn, scenario_version='wrong'))
    assert factory.store.load('source-clock') == before
    assert len(capture.requests) == 1


def test_campaign_factory_inspection_does_not_reuse_programless_engine(portal, tmp_path):
    factory = portal.gameplay.factory
    item = replace(portal.gameplay.game_access.listing_for('unused', 'npc-chat'), package_path=tmp_path)
    package = ScenarioPackage.load(portal.gameplay.game_access.listing_for('unused', 'npc-chat').package_path)
    program_data = {'id': 'inspection', 'ticks_per_day': 4, 'final_tick': 8, 'steps': [
        {'id': 'scene', 'kind': 'scene', 'at': 0, 'text': 'An inspected harbor scene',
         'action_goal': 'Find the arriving boat', 'action_anchors': ['The harbor is open']},
        {'id': 'gate', 'kind': 'choice', 'at': 7, 'prompt': 'Choose', 'options': [{'id': 'yes', 'label': 'Yes'}]},
        {'id': 'end', 'kind': 'finale', 'at': 8, 'choice_key': 'gate', 'threshold': 1,
         'success_text': 'Success', 'other_text': 'Done'}]}
    (tmp_path / 'campaign.json').write_text(json.dumps(program_data), encoding='utf-8')
    from copy import deepcopy
    state = deepcopy(package.initial_state)
    state['campaign'] = {'cursor': 1, 'day': 1}
    package = replace(package, initial_state=state)
    package.seed_game(factory.store, 'program')
    capture = CaptureModel()
    factory.offline_models.model = capture
    plain = factory.engine(item, package, 'program', overnight_requires_rest=True)
    program, campaign = factory.campaign(item, package, 'program')
    engine = campaign.turn_engine
    assert engine is not plain
    turn = TurnInput('program', 'hello', 'program-turn', package.version)
    projection = engine.project_context(turn)
    assert projection.request['current_story_scene'] == program_data['steps'][0]['text']
    assert projection.request['current_story_goal'] == program_data['steps'][0]['action_goal']
    assert projection.request['story_anchors'] == program_data['steps'][0]['action_anchors']
    assert not capture.requests
    asyncio.run(engine.run_turn(turn))
    assert capture.requests[0] == projection.request


def test_actual_factory_output_cap_reaches_offline_sdk_wire(portal):
    factory = portal.gameplay.factory
    configure(factory, output=321)
    factory.models = ModelFactory(factory.settings, env={'STORY_MODEL_API_KEY': 'offline-test'})
    item = portal.gameplay.game_access.listing_for('unused', 'npc-chat')
    package = ScenarioPackage.load(item.package_path)
    package.seed_game(factory.store, 'sdk')
    model = factory.models.create_model('single_turn')
    capture = []

    async def completion(**kwargs):
        capture.append(kwargs)
        decision = {'prose': 'The dockhand waves.', 'participants': ['dockhand'],
                    'replies': [{'actor_id': 'dockhand', 'speech': 'Hello.'}]}
        return ChatCompletion.model_validate({'id': 'offline', 'created': 0, 'model': 'offline',
            'object': 'chat.completion', 'usage': {'prompt_tokens': 7, 'completion_tokens': 3, 'total_tokens': 10},
            'choices': [{'finish_reason': 'tool_calls', 'index': 0, 'message': {'role': 'assistant', 'content': None,
                'tool_calls': [{'id': 'output', 'type': 'function', 'function': {
                    'name': 'generate_structured_output', 'arguments': json.dumps(decision)}}]}}]})
    model.client.chat.completions.create = AsyncMock(side_effect=completion)
    factory.models.create_model = lambda task, **kwargs: model
    engine = factory.engine(item, package, 'sdk')
    asyncio.run(engine.run_turn(TurnInput('sdk', 'hello', 'sdk-turn', package.version)))
    assert len(capture) == 1
    wire = capture[0]
    assert wire['max_completion_tokens'] == 321
    assert 'max_tokens' not in wire and 'context_budget' not in wire
    serialized = json.dumps(wire, default=str)
    assert 'ContextBudget(' not in serialized and 'ProjectionReport(' not in serialized
    assert wire['tools'] and '$ref' not in json.dumps(wire['tools'])
