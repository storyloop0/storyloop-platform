import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from storyloop_platform.portal.http_api import create_app

from storyloop_harness.advanced import PendingWork
from storyloop_platform.config import load_settings
from test_turn_failure_contract import portal, ROOT


def test_only_active_routes_and_runtime_limits_are_required(tmp_path):
    raw = json.loads((ROOT / 'config/local.json').read_text(encoding='utf-8'))
    raw['routes'] = {task: 'story' for task in
                              ('single_turn', 'narration', 'followup_actions')}
    raw.setdefault('runtime', {}).pop('main_max_iters', None)
    raw['runtime'].pop('npc_max_iters', None)
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(raw), encoding='utf-8')
    config = load_settings(path)
    assert config.model_for('single_turn').model == 'deepseek-v4.1-flash'
    assert not hasattr(config.runtime, 'npc_max_iters')


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('due_tick', [0, 100])
def test_obsolete_work_rejected_before_model_world_or_billing(portal, monkeypatch, stream, due_tick):
    token = portal.register('reader', 'password-123')['token']
    game_id = asyncio.run(portal.create_save(token, 'npc-chat'))['game_id']
    before = portal.gameplay.store.load(game_id)
    work = PendingWork('obsolete', 'npc_reply', due_tick, 10, None,
                       {'actor_id': 'dockhand', 'player_message': 'hello'})
    portal.gameplay.store.commit(game_id, before.version, None, (), (work,))
    snapshot = portal.gameplay.store.load(game_id)
    queued = portal.gameplay.store.pending_work(game_id)
    observations = portal.gameplay.store.observations_for(game_id, 'player')
    ledger = portal.credit_ledger(token)
    def forbidden(*args, **kwargs):
        raise AssertionError('obsolete saves must fail before model or billing work')
    monkeypatch.setattr(type(portal.gameplay.factory.models), 'create_model', forbidden)
    monkeypatch.setattr(portal.billing, 'require_credit', forbidden)
    with TestClient(create_app(portal), base_url='http://127.0.0.1') as client:
        response = client.post(f'/v1/saves/{game_id}/turns' + ('/stream' if stream else ''),
            headers={'Authorization': f'Bearer {token}'},
            json={'text': 'hello', 'request_id': 'obsolete'})
        payload = ([json.loads(line[6:]) for line in response.text.splitlines()
                    if line.startswith('data: ')][-1] if stream else response.json())
        assert 'new game' in str(payload)
    assert portal.gameplay.store.load(game_id) == snapshot
    assert portal.gameplay.store.pending_work(game_id) == queued
    assert portal.gameplay.store.observations_for(game_id, 'player') == observations
    assert portal.credit_ledger(token) == ledger
    assert portal.turns.settlements.get(game_id, 'obsolete', 'hello') is None


def test_current_http_turn_uses_scene_projection_and_settles_once_with_only_active_routes(portal, monkeypatch):
    from runtime_fakes import StructuredOfflineModel

    portal.settings = portal.settings.model_copy(update={'routes': {task: 'story'
        for task in ('single_turn', 'narration', 'followup_actions')}})
    portal.gameplay.factory.settings = portal.settings
    model = StructuredOfflineModel(lambda _: {
        'beats': [{'kind': 'narration', 'text': '你听见码头工放下缆绳。'},
                  {'kind': 'dialogue', 'actor_id': 'dockhand', 'text': '今天有船靠岸。'}],
        'participants': ['dockhand'],
    }, input_tokens=100, output_tokens=50)
    portal.gameplay.factory.offline_models.model = model
    token = portal.register('scene-reader', 'password-123')['token']
    game_id = asyncio.run(portal.create_save(token, 'npc-chat'))['game_id']
    with TestClient(create_app(portal), base_url='http://127.0.0.1') as client:
        args = dict(headers={'Authorization': f'Bearer {token}'},
                    json={'text': 'hello', 'request_id': 'scene-one'})
        first = client.post(f'/v1/saves/{game_id}/turns', **args)
        replay = client.post(f'/v1/saves/{game_id}/turns', **args)
        assert first.status_code == replay.status_code == 200
        assert first.json() == replay.json()
        assert first.json()['billing']['input_tokens'] == 100
    assert len(model.requests) == 1
    assert 'npc_contexts' in model.requests[0]
    assert portal.gameplay.store.event_exists(game_id, 'portal-scene-one:input:reply:dockhand:spoken')
    assert len([entry for entry in portal.credit_ledger(token) if entry['kind'] == 'turn']) == 1
