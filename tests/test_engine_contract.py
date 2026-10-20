"""Platform consumes narrative status committed by the public engine once."""
import asyncio
from dataclasses import replace
import pytest

from storyloop_harness import ScenarioPackage, TurnEngine, TurnInput
from storyloop_platform.adapters.store import SQLiteGameStore
from runtime_fakes import StructuredOfflineModel
from scenario_fixtures import EXAMPLE, status_package


@pytest.fixture
def world(tmp_path):
    store = SQLiteGameStore(str(tmp_path / 'world.sqlite3'))
    package = replace(ScenarioPackage.load(EXAMPLE), initial_work=())
    package.seed_game(store, 'game')
    return store, package


@pytest.mark.parametrize('delta', [1, 99])
def test_plain_status_change_is_validated_before_prose_commit_and_applied_once(world, delta):
    store, base = world
    package = status_package(base)
    package.seed_game(store, 'status')
    model = StructuredOfflineModel(lambda _: {'beats': [
        {'kind': 'narration', 'text': '你感到压力增加。'}],
        'status_changes': [{'id': 'pressure', 'delta': delta}]})
    engine = TurnEngine(store, package, model)

    async def run():
        before = store.load('status')
        request = TurnInput('status', '我有些紧张', 'turn', package.version)
        if delta == 99:
            with pytest.raises(ValueError, match='delta'):
                await engine.run_turn(request)
            assert store.load('status').version == before.version
            assert not store.event_exists('status', 'turn:input')
            return
        first = (await engine.run_turn(request)).snapshot
        again = (await TurnEngine(store, package, model).run_turn(request)).snapshot
        assert first.data['player_stats']['pressure'] == 4
        assert again.version == first.version
        assert len(model.requests) == 1
        assert not store.event_exists('status', 'turn:status')
    asyncio.run(run())
