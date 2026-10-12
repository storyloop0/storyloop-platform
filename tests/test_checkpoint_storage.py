"""Platform SQL store implements the Harness checkpoint contract."""
import asyncio
from threading import Event, Thread

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import event as sqlalchemy_event, inspect, text

from storyloop_harness import TurnEngine, TurnInput
from storyloop_harness.advanced import AgentContextCheckpoint, Observation, WorldEvent
from storyloop_harness.context import CheckpointLifecycle
from storyloop_harness.testing import OfflineModel
from storyloop_platform.adapters.sql_database import open_database, sqlite_url, upgrade_database
from storyloop_platform.adapters.sql_store import SQLGameStore
from storyloop_platform.adapters.store import SQLiteGameStore
from scenario_fixtures import source_package


def history(store, package, game_id, count):
    package.seed_game(store, game_id)
    for index in range(count):
        event = WorldEvent(f'{game_id}-event-{index}', 'synthetic', None, None, index, ())
        store.commit(game_id, store.load(game_id).version, event, (
            Observation(f'{game_id}-guide-{index}', event.event_id, 'guide',
                        'private_message', f'GUIDE_PRIVATE {index}', index),
            Observation(f'{game_id}-player-{index}', event.event_id, 'player',
                        'scene', 'MIDDLE_CLUE' if index == 3 else f'ordinary {index}', index),
        ), ())


def test_upgrade_discards_only_unverifiable_old_checkpoint_cache(tmp_path):
    path = tmp_path / 'migration.db'
    url = sqlite_url(str(path))
    config = Config()
    config.set_main_option('script_location', str(__import__('pathlib').Path(
        __file__).resolve().parents[1] / 'src/storyloop_platform/migrations'))
    config.set_main_option('sqlalchemy.url', url)
    command.upgrade(config, '0014_observation_join_index')
    engine = open_database(url)
    try:
        store = SQLGameStore(engine)
        package = source_package()
        history(store, package, 'game', 1)
        with engine.begin() as db:
            db.execute(text("""INSERT INTO agent_contexts
                (game_id,actor_id,through_version,summary)
                VALUES ('game','guide',1,'legacy private cache')"""))
    finally:
        engine.dispose()

    upgrade_database(url)
    engine = open_database(url)
    try:
        columns = {column['name'] for column in inspect(engine).get_columns('agent_contexts')}
        assert {'scenario_id', 'scenario_version', 'source_ids'} <= columns
        with engine.connect() as db:
            assert db.execute(text('SELECT COUNT(*) FROM agent_contexts')).scalar_one() == 0
            assert db.execute(text('SELECT COUNT(*) FROM events')).scalar_one() == 1
            assert db.execute(text('SELECT COUNT(*) FROM observations')).scalar_one() == 2
            assert db.execute(text('SELECT version FROM games WHERE game_id=:game'), {'game': 'game'}).scalar_one() == 1
    finally:
        engine.dispose()


def test_sql_store_lifecycle_reopen_and_late_observation_guard(tmp_path):
    async def run():
        package = source_package()
        path = tmp_path / 'long.db'
        store = SQLiteGameStore(str(path))
        try:
            history(store, package, 'one', 24)
            history(store, package, 'two', 1)

            async def summarize(request):
                return request.previous_summary + ' | ' + ' '.join(
                    item.content for item in request.entries)

            lifecycle = CheckpointLifecycle(store, package, summarize)
            assert await lifecycle.refresh('one', 'guide')
            checkpoint = store.agent_context_checkpoint('one', 'guide')
            assert checkpoint.through_version == 16
            assert checkpoint.scenario_id == package.package_id
            assert checkpoint.scenario_version == package.version
            assert checkpoint.source_ids == tuple(f'one-guide-{index}' for index in range(16))
            assert 'MIDDLE_CLUE' not in checkpoint.summary
            assert store.agent_context_checkpoint('two', 'guide') == AgentContextCheckpoint()
            assert len(store.agent_context_entries('one', 'guide')) == 24
            with pytest.raises(ValueError, match='checkpointed event'):
                store.commit('one', store.load('one').version, None, (
                    Observation('late', 'one-event-0', 'guide', 'scene', 'late', 24),), ())
            assert all(entry.entry_id != 'late' for entry in store.agent_context_entries('one', 'guide'))
            assert not await lifecycle.refresh('one', 'guide')
        finally:
            store.close()
        reopened = SQLiteGameStore(str(path))
        try:
            assert reopened.agent_context_checkpoint('one', 'guide') == checkpoint
        finally:
            reopened.close()

    asyncio.run(run())


def test_sql_store_rejects_invalid_cursor_source_and_scenario(tmp_path):
    package = source_package()
    store = SQLiteGameStore(str(tmp_path / 'scope.db'))
    try:
        history(store, package, 'game', 1)
        valid = AgentContextCheckpoint(1, 'summary', package.package_id,
                                       package.version, ('game-guide-0',))
        with pytest.raises(ValueError, match='cursor'):
            store.save_agent_context_checkpoint('game', 'guide', -1,
                AgentContextCheckpoint(2, 'summary', package.package_id,
                                       package.version, ('game-guide-0',)))
        with pytest.raises(ValueError, match='source'):
            store.save_agent_context_checkpoint('game', 'guide', -1,
                AgentContextCheckpoint(1, 'summary', package.package_id,
                                       package.version, ('wrong',)))
        with pytest.raises(ValueError, match='scenario'):
            store.save_agent_context_checkpoint('game', 'guide', -1,
                AgentContextCheckpoint(1, 'summary', 'other', package.version,
                                       ('game-guide-0',)))
        store.save_agent_context_checkpoint('game', 'guide', -1, valid)
        with pytest.raises(ValueError, match='changed'):
            store.save_agent_context_checkpoint('game', 'guide', -1, valid)
    finally:
        store.close()


def test_checkpoint_and_observation_writer_cannot_cross_after_cursor_read(tmp_path):
    package = source_package()
    path = tmp_path / 'concurrent.db'
    writer = SQLiteGameStore(str(path))
    saver = SQLiteGameStore(str(path))
    try:
        history(writer, package, 'game', 1)
        checkpoint = AgentContextCheckpoint(1, 'summary', package.package_id,
                                            package.version, ('game-guide-0',))
        cursor_read = Event()
        resume_writer = Event()
        saver_done = Event()
        errors = []

        def pause_after_cursor_read(connection, cursor, statement, parameters, context,
                                    executemany):
            if 'SELECT through_version FROM agent_contexts' in statement:
                cursor_read.set()
                assert resume_writer.wait(10)

        sqlalchemy_event.listen(writer.engine, 'after_cursor_execute', pause_after_cursor_read)

        def write_observation():
            try:
                writer.commit('game', 1, None, (
                    Observation('late', 'game-event-0', 'guide', 'scene', 'late', 1),), ())
            except Exception as error:
                errors.append(error)

        def save_checkpoint():
            try:
                saver.save_agent_context_checkpoint('game', 'guide', -1, checkpoint)
            except Exception as error:
                errors.append(error)
            finally:
                saver_done.set()

        writing = Thread(target=write_observation)
        saving = Thread(target=save_checkpoint)
        writing.start()
        assert cursor_read.wait(10)
        saving.start()
        saver_done.wait(1)
        resume_writer.set()
        writing.join(10)
        saving.join(10)
        assert not writing.is_alive() and not saving.is_alive()
        assert any(entry.entry_id == 'late' for entry in writer.agent_context_entries('game', 'guide'))
        assert writer.agent_context_checkpoint('game', 'guide') == AgentContextCheckpoint()
        assert len(errors) == 1 and isinstance(errors[0], ValueError)
        assert 'source' in str(errors[0])
    finally:
        writer.close()
        saver.close()


def test_context_read_keeps_event_and_observation_in_one_view(tmp_path):
    path = tmp_path / 'read.db'
    reader = SQLiteGameStore(str(path))
    writer = SQLiteGameStore(str(path))
    try:
        source_package().seed_game(writer, 'game')
        injected = False

        def write_after_first_query(connection, cursor, statement, parameters, context,
                                    executemany):
            nonlocal injected
            if injected or 'FROM events AS e WHERE' not in statement:
                return
            injected = True
            event = WorldEvent('event', 'player_input', 'player', None, 1, (),
                               {'text': 'hello'})
            writer.commit('game', 0, event, (
                Observation('observation', 'event', 'player', 'scene', 'scene', 1),), ())

        sqlalchemy_event.listen(reader.engine, 'after_cursor_execute', write_after_first_query)
        entries = reader.agent_context_entries('game', 'player')
        assert injected
        ids = {entry.entry_id for entry in entries}
        assert 'observation' not in ids or 'event' in ids
    finally:
        reader.close()
        writer.close()


def test_platform_store_runs_opt_in_engine_checkpoint(tmp_path):
    async def run():
        package = source_package()
        store = SQLiteGameStore(str(tmp_path / 'engine.db'))
        try:
            history(store, package, 'game', 24)
            calls = []

            async def summarize(request):
                calls.append(request.actor_id)
                return 'remember ' + request.entries[0].content

            engine = TurnEngine(store, package, OfflineModel(), checkpoint_summarizer=summarize)
            turn = TurnInput('game', 'Guide hello', 'turn-1', package.version)
            outcome = await engine.run_turn(turn)
            assert outcome.snapshot.version > 24
            assert store.agent_context_checkpoint('game', 'guide').through_version >= 16
            before_replay = tuple(calls)
            replay = await engine.run_turn(turn)
            assert replay.model_usage == ()
            assert tuple(calls) == before_replay
        finally:
            store.close()

    asyncio.run(run())
