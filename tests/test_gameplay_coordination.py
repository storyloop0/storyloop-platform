"""Event-controlled shared coordination across the two gameplay services."""

import asyncio

from runtime_fakes import OfflineExecutor, turn_outcome
from test_turn_failure_contract import portal
from test_player_memory import FakePlayerMemory


def test_turn_blocks_settings_and_resume_until_receipt_is_persisted(portal, monkeypatch):
    token = portal.register("coordinated", "password-123")["token"]
    game_id = asyncio.run(portal.create_save(token, "npc-chat"))["game_id"]
    original_locks = portal.gameplay.player_locks.for_player

    async def run():
        entered = asyncio.Event()
        release = asyncio.Event()
        attempts = {name: asyncio.Event() for name in ("settings", "resume")}
        class ObservedLock:
            def __init__(self, lock):
                self.lock = lock
            async def __aenter__(self):
                name = asyncio.current_task().get_name()
                if name in attempts:
                    attempts[name].set()
                return await self.lock.__aenter__()
            async def __aexit__(self, *args):
                return await self.lock.__aexit__(*args)
        monkeypatch.setattr(portal.gameplay.player_locks, "for_player",
                            lambda player: ObservedLock(original_locks(player)))
        class Executor(OfflineExecutor):
            def proposed_options(self, *args):
                return ()
            async def run_turn(self, turn, **kwargs):
                entered.set()
                await release.wait()
                return turn_outcome(portal.gameplay.store.load(turn.game_id), "completed under lock")
        portal.gameplay.factory.executor_builder = lambda item, package, *args, **kwargs: Executor(portal.gameplay.store, package)
        turn = asyncio.create_task(portal.turn(token, game_id, "hello", "coordinated"), name="turn")
        await entered.wait()
        settings = asyncio.create_task(portal.set_save_settings(token, game_id, 0.8, 32768), name="settings")
        resume = asyncio.create_task(portal.resume_save(token, game_id), name="resume")
        await attempts["settings"].wait()
        await attempts["resume"].wait()
        assert not settings.done() and not resume.done()
        assert portal.get_save_settings(token, game_id)["temperature"] == 1.0
        release.set()
        response, updated, resumed = await asyncio.gather(turn, settings, resume)
        assert updated["temperature"] == 0.8
        assert resumed["body"] == response["body"] == "completed under lock"
        assert portal.accounts.get_turn_response(game_id, "coordinated", "hello") == response

    asyncio.run(run())


def test_turn_started_before_preference_correction_does_not_requeue_old_input(portal):
    token = portal.register("memory-race", "password-123")["token"]
    player_id = portal.accounts.resolve_token(token)
    game_id = asyncio.run(portal.create_save(token, "npc-chat"))["game_id"]
    fake = FakePlayerMemory()
    fake.rows[player_id] = [{"id": "memory", "text": "喜欢快节奏", "created_at": ""}]
    portal.memory_service.provider = fake
    portal.memory_service.enabled = True

    async def run():
        await portal.set_memory_enabled(token, True)
        entered, release = asyncio.Event(), asyncio.Event()

        class Executor(OfflineExecutor):
            def proposed_options(self, *args):
                return ()

            async def run_turn(self, turn, **kwargs):
                entered.set()
                await release.wait()
                return turn_outcome(portal.gameplay.store.load(turn.game_id), "completed after correction")

        portal.gameplay.factory.executor_builder = lambda item, package, *args, **kwargs: Executor(portal.gameplay.store, package)
        turn = asyncio.create_task(portal.turn(token, game_id, "我希望这个故事的节奏一直快一点。", "before-correction"))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            corrected = await portal.correct_memory(token, "memory", "喜欢慢节奏")
            assert corrected["memories"][0]["text"] == "喜欢慢节奏"
        finally:
            release.set()
            await turn
        assert portal.memory_service.jobs.pending_count(player_id) == 0

    asyncio.run(run())
