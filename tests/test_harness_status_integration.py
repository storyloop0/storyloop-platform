"""The app displays committed Harness status without a second status writer."""
import asyncio

from storyloop_harness import ScenarioPackage
from runtime_fakes import StructuredOfflineModel
from scenario_fixtures import EXAMPLE, status_package
from test_turn_failure_contract import portal


def test_portal_commits_status_once_and_replays_receipt(portal, monkeypatch, caplog):
    package = status_package(ScenarioPackage.load(EXAMPLE))
    monkeypatch.setattr(ScenarioPackage, 'load', classmethod(lambda cls, path: package))
    model = StructuredOfflineModel(lambda _: {
        'prose': '你感到压力增加。',
        'status_changes': [{'id': 'pressure', 'delta': 1}],
    })
    portal.gameplay.factory.offline_models.model = model
    token = portal.register('status-reader', 'password-123')['token']
    game = asyncio.run(portal.create_save(token, 'npc-chat'))['game_id']
    result = asyncio.run(portal.turn(token, game, '我有些紧张', 'status-once'))
    replay = asyncio.run(portal.turn(token, game, '我有些紧张', 'status-once'))
    assert replay == result
    assert portal.gameplay.store.load(game).data['player_stats']['pressure'] == 4
    assert next(row for row in result['status_fields'] if row['id'] == 'pressure')['value'] == 4
    assert len(model.requests) == 1
    assert not portal.gameplay.store.event_exists(game, 'portal-status-once:status')
    assert not any('status review failed' in record.message for record in caplog.records)
