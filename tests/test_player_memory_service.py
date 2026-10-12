"""Player memory policy is testable without auth, a database, or model calls."""

import asyncio
import threading
from contextlib import nullcontext

import pytest

from storyloop_platform.lifecycle import OperationSupervisor
from storyloop_platform.memory.jobs import MemoryBatch
from storyloop_platform.memory.service import PlayerMemoryService


class FakeMemory:
    def __init__(self):
        self.rows = {}
        self.reads = 0
        self.closed = False
        self.error = False
        self.remembered = []

    def list_memories(self, player_id):
        self.reads += 1
        if self.error:
            raise RuntimeError("read failed")
        return self.rows.get(player_id, [])

    def remember(self, player_id, inputs):
        if self.error:
            raise RuntimeError("extraction failed")
        self.remembered.append((player_id, inputs))

    def clear(self, player_id):
        self.rows.pop(player_id, None)

    def correct(self, player_id, memory_id, text):
        for row in self.rows.get(player_id, []):
            if row.get("id") == memory_id:
                row["text"] = text
                row["source"] = "user_correction"
                return
        raise KeyError("memory not found")

    def delete_one(self, player_id, memory_id):
        rows = self.rows.get(player_id, [])
        if not any(row.get("id") == memory_id for row in rows):
            raise KeyError("memory not found")
        self.rows[player_id] = [row for row in rows if row.get("id") != memory_id]

    def close(self):
        self.closed = True


class FakeJobs:
    def __init__(self):
        self.settings = set()
        self.inputs = []
        self.batches = []
        self.completed = []
        self.failed = []
        self.settled = threading.Event()

    def enabled(self, player_id):
        return player_id in self.settings

    def set_enabled(self, player_id, enabled):
        if enabled:
            self.settings.add(player_id)
        else:
            self.settings.discard(player_id)
            self.clear(player_id)

    def enqueue(self, player_id, game_id, request_id, player_text):
        if not self.enabled(player_id):
            return False
        self.inputs.append((player_id, game_id, request_id, player_text))
        return True

    def pending_count(self, player_id):
        return sum(item[0] == player_id for item in self.inputs)

    def clear(self, player_id):
        self.inputs = [item for item in self.inputs if item[0] != player_id]

    def claim(self):
        return self.batches.pop(0) if self.batches else None

    def complete(self, batch):
        self.completed.append(batch)
        self.settled.set()

    def fail(self, batch):
        self.failed.append(batch)
        self.settled.set()


class FakeTelemetry:
    def __init__(self):
        self.spans = []
        self.metrics = []

    def span(self, name, metadata, **kwargs):
        self.spans.append((name, metadata, kwargs))
        return nullcontext(self)

    def metric(self, name, value):
        self.metrics.append((name, value))


def test_status_collection_cache_and_clear_are_player_scoped():
    async def run():
        memory, jobs = FakeMemory(), FakeJobs()
        service = PlayerMemoryService(memory, jobs, True, FakeTelemetry(), OperationSupervisor())
        memory.rows["first"] = [{"text": "slow pacing"}]
        assert (await service.status("first"))["enabled"] is False
        service.queue_input("first", "game", "before", "I prefer slow pacing")
        assert jobs.inputs == []
        assert (await service.set_enabled("first", True))["enabled"] is True
        service.queue_input("first", "game", "request", "I prefer slow pacing")
        assert (await service.status("first"))["queued_inputs"] == 1
        assert await service.preferences_for("first") == ("slow pacing",)
        reads = memory.reads
        assert await service.preferences_for("first") == ("slow pacing",)
        assert memory.reads == reads
        assert await service.preferences_for("second") == ()
        await service.set_enabled("first", False)
        assert jobs.inputs == []
        memory.rows["first"] = [{"text": "new preference"}]
        await service.set_enabled("first", True)
        assert await service.preferences_for("first") == ("new preference",)
        service.enabled = False
        status = await service.status("first")
        assert status["available"] is False and status["memories"]
        assert status["charged_points"] == 0
        assert await service.preferences_for("first") == ()
        service.queue_input("first", "game", "disabled", "I prefer slower pacing")
        assert jobs.pending_count("first") == 0
        with pytest.raises(ValueError):
            await service.set_enabled("first", True)
        assert (await service.clear("first"))["memories"] == []
        assert jobs.pending_count("first") == 0
        service.enabled = True
        assert await service.preferences_for("first") == ()
        service.close()
        assert memory.closed
    asyncio.run(run())


def test_preferences_are_bounded_and_optional_read_failure_degrades_safely():
    async def run():
        memory, jobs = FakeMemory(), FakeJobs()
        jobs.set_enabled("player", True)
        service = PlayerMemoryService(memory, jobs, True, FakeTelemetry(), OperationSupervisor())
        memory.error = True
        assert await service.preferences_for("player") == ()
        memory.error = False
        memory.rows["player"] = [{"text": "a" * 200}] * 10
        assert await service.preferences_for("player") == ("a" * 180,) * 8
    asyncio.run(run())


def test_campaign_controls_are_not_collected():
    memory, jobs = FakeMemory(), FakeJobs()
    jobs.set_enabled("player", True)
    service = PlayerMemoryService(memory, jobs, True, FakeTelemetry(), OperationSupervisor())
    for text in ("/continue", "/next", "/rest", "/choose a 一句话"):
        service.queue_input("player", "game", text, text)
    service.queue_input("player", "game", "speech", "我喜欢慢慢认识人。")
    assert jobs.inputs == [("player", "game", "speech", "我喜欢慢慢认识人。")]


@pytest.mark.parametrize("fails", [False, True])
def test_worker_settles_extraction_and_does_not_bill_player(fails):
    async def run():
        memory, jobs, telemetry = FakeMemory(), FakeJobs(), FakeTelemetry()
        jobs.set_enabled("player", True)
        memory.rows["player"] = [{"text": "old preference"}]
        batch = MemoryBatch("player", tuple(("game", str(i), "slow pacing") for i in range(6)))
        jobs.batches.append(batch)
        service = PlayerMemoryService(memory, jobs, True, telemetry, OperationSupervisor())
        assert await service.preferences_for("player") == ("old preference",)
        if not fails:
            memory.rows["player"] = [{"text": "new preference"}]
        memory.error = fails
        worker = asyncio.create_task(service.run_worker())
        try:
            assert await asyncio.to_thread(jobs.settled.wait, 5)
        finally:
            worker.cancel()
            with pytest.raises(asyncio.CancelledError):
                await worker
        assert jobs.failed == ([batch] if fails else [])
        assert jobs.completed == ([] if fails else [batch])
        memory.error = False
        assert await service.preferences_for("player") == (("old preference",) if fails else ("new preference",))
        assert telemetry.spans[0][1]["billed_to_player"] is False
        assert telemetry.metrics == [("story.player_memory_batch_success", float(not fails))]
    asyncio.run(run())


def test_worker_shutdown_finishes_claimed_batch_before_close():
    async def run():
        memory, jobs = FakeMemory(), FakeJobs()
        started, finish = threading.Event(), threading.Event()
        def remember(player_id, inputs):
            started.set()
            assert finish.wait(5)
            assert not memory.closed
        memory.remember = remember
        batch = MemoryBatch("player", (("game", "request", "preference"),))
        jobs.batches.append(batch)
        service = PlayerMemoryService(memory, jobs, True, FakeTelemetry(), OperationSupervisor())
        worker = asyncio.create_task(service.run_worker())
        try:
            assert await asyncio.to_thread(started.wait, 5)
            worker.cancel()
            await asyncio.sleep(0)
            assert not worker.done()
        finally:
            finish.set()
            with pytest.raises(asyncio.CancelledError):
                await worker
        assert jobs.completed == [batch]
        service.close()
        assert memory.closed
    asyncio.run(run())


def test_missing_provider_disables_worker_and_collection():
    async def run():
        jobs = FakeJobs()
        service = PlayerMemoryService(None, jobs, True, FakeTelemetry(), OperationSupervisor())
        assert (await service.status("player"))["available"] is False
        assert await service.preferences_for("player") == ()
        service.queue_input("player", "game", "request", "a long preference input")
        assert jobs.inputs == []
        await service.run_worker()
        with pytest.raises(ValueError):
            await service.clear("player")
        service.close()
    asyncio.run(run())


def test_idle_worker_stop_is_explicit_and_prompt():
    async def run():
        service = PlayerMemoryService(FakeMemory(), FakeJobs(), True, FakeTelemetry(), OperationSupervisor())
        worker = asyncio.create_task(service.run_worker())
        try:
            service.request_stop()
            await asyncio.wait_for(worker, 0.5)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
    asyncio.run(run())


def test_repeated_worker_cancellation_preserves_real_batch_and_close_guard():
    async def run():
        memory, jobs = FakeMemory(), FakeJobs()
        started, finish = threading.Event(), threading.Event()
        def remember(*args):
            started.set()
            assert finish.wait(5)
            assert not memory.closed
        memory.remember = remember
        batch = MemoryBatch("player", (("game", "request", "preference"),))
        jobs.batches.append(batch)
        service = PlayerMemoryService(memory, jobs, True, FakeTelemetry(), OperationSupervisor())
        worker = asyncio.create_task(service.run_worker())
        try:
            assert await asyncio.to_thread(started.wait, 2)
            worker.cancel()
            await asyncio.sleep(0)
            worker.cancel()
            await asyncio.sleep(0.01)
            assert not worker.done()
            with pytest.raises(RuntimeError, match="active"):
                service.close()
        finally:
            finish.set()
            await asyncio.gather(worker, return_exceptions=True)
        assert jobs.completed == [batch]
        service.close()
        assert memory.closed
    asyncio.run(run())


@pytest.mark.parametrize("mutation", ["clear", "disable", "disable_enable", "batch"])
def test_inflight_preferences_cannot_publish_after_player_mutation(mutation):
    async def run():
        memory, jobs = FakeMemory(), FakeJobs()
        jobs.set_enabled("player", True)
        memory.rows["player"] = [{"text": "old preference"}]
        captured, release = threading.Event(), threading.Event()
        original_list = memory.list_memories

        def held_snapshot(player_id):
            rows = list(original_list(player_id))
            if player_id == "player" and not captured.is_set():
                captured.set()
                assert release.wait(5)
            return rows

        memory.list_memories = held_snapshot
        service = PlayerMemoryService(memory, jobs, True, FakeTelemetry(), OperationSupervisor())
        read = asyncio.create_task(service.preferences_for("player"))
        try:
            assert await asyncio.to_thread(captured.wait, 5)
            if mutation == "clear":
                await service.clear("player")
            elif mutation.startswith("disable"):
                await service.set_enabled("player", False)
                if mutation == "disable_enable":
                    memory.rows["player"] = [{"text": "new preference"}]
                    await service.set_enabled("player", True)
            else:
                batch = MemoryBatch("player", (("game", "request", "preference"),))
                jobs.batches.append(batch)
                memory.remember = lambda *args: memory.rows.update(
                    {"player": [{"text": "new preference"}]})
                assert await service._run_batch() == batch
            # A stalled read for one player does not prevent another player's read.
            jobs.set_enabled("other", True)
            memory.rows["other"] = [{"text": "other preference"}]
            assert await service.preferences_for("other") == ("other preference",)
        finally:
            release.set()
            old_result = await read
        assert old_result == ()
        expected = ("new preference",) if mutation in {"disable_enable", "batch"} else ()
        assert await service.preferences_for("player") == expected
        service.close()
    asyncio.run(run())


@pytest.mark.parametrize("outcome", ["cancel", "error"])
def test_clear_invalidates_cache_when_provider_finishes_without_normal_return(outcome):
    async def run():
        memory, jobs = FakeMemory(), FakeJobs()
        jobs.set_enabled("player", True)
        memory.rows["player"] = [{"text": "old preference"}]
        cleared, release = threading.Event(), threading.Event()

        def held_clear(player_id):
            memory.rows.pop(player_id, None)
            cleared.set()
            assert release.wait(5)
            if outcome == "error":
                raise RuntimeError("cleanup failed after deletion")

        service = PlayerMemoryService(memory, jobs, True, FakeTelemetry(), OperationSupervisor())
        assert await service.preferences_for("player") == ("old preference",)
        memory.clear = held_clear
        clearing = asyncio.create_task(service.clear("player"))
        try:
            assert await asyncio.to_thread(cleared.wait, 5)
            if outcome == "cancel":
                clearing.cancel()
                with pytest.raises(RuntimeError, match="active"):
                    service.close()
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError if outcome == "cancel" else RuntimeError):
                await clearing
        assert await service.preferences_for("player") == ()
        service.close()
    asyncio.run(run())


def test_failed_extraction_invalidates_cache_after_partial_provider_write():
    async def run():
        memory, jobs = FakeMemory(), FakeJobs()
        jobs.set_enabled("player", True)
        memory.rows["player"] = [{"text": "old preference"}]
        service = PlayerMemoryService(memory, jobs, True, FakeTelemetry(), OperationSupervisor())
        assert await service.preferences_for("player") == ("old preference",)

        def partial_remember(*args):
            memory.rows["player"] = [{"text": "new preference"}]
            raise RuntimeError("extraction failed after write")

        memory.remember = partial_remember
        batch = MemoryBatch("player", (("game", "request", "preference"),))
        jobs.batches.append(batch)
        assert await service._run_batch() == batch
        assert jobs.failed == [batch]
        assert await service.preferences_for("player") == ("new preference",)
        service.close()
    asyncio.run(run())


def test_read_started_during_clear_cannot_repopulate_after_clear_settles():
    async def run():
        memory, jobs = FakeMemory(), FakeJobs()
        jobs.set_enabled("player", True)
        memory.rows["player"] = [{"text": "old preference"}]
        clearing_started, finish_clear = threading.Event(), threading.Event()
        snapshot_taken, finish_read = threading.Event(), threading.Event()
        original_list = memory.list_memories

        def held_clear(player_id):
            clearing_started.set()
            assert finish_clear.wait(5)
            memory.rows.pop(player_id, None)

        def held_snapshot(player_id):
            rows = list(original_list(player_id))
            if not snapshot_taken.is_set():
                snapshot_taken.set()
                assert finish_read.wait(5)
            return rows

        memory.clear, memory.list_memories = held_clear, held_snapshot
        service = PlayerMemoryService(memory, jobs, True, FakeTelemetry(), OperationSupervisor())
        clearing = asyncio.create_task(service.clear("player"))
        read = None
        try:
            assert await asyncio.to_thread(clearing_started.wait, 5)
            read = asyncio.create_task(service.preferences_for("player"))
            assert await asyncio.to_thread(snapshot_taken.wait, 5)
            finish_clear.set()
            assert (await clearing)["memories"] == []
        finally:
            finish_clear.set()
            finish_read.set()
            await asyncio.gather(clearing, return_exceptions=True)
            if read is not None:
                old_result = await read
        assert old_result == ()
        assert await service.preferences_for("player") == ()
        service.close()
    asyncio.run(run())


def test_single_record_controls_scope_queue_and_cache():
    async def run():
        memory, jobs = FakeMemory(), FakeJobs()
        jobs.set_enabled("first", True)
        jobs.set_enabled("second", True)
        memory.rows["first"] = [{"id": "a", "text": "old preference"}]
        memory.rows["second"] = [{"id": "b", "text": "other preference"}]
        service = PlayerMemoryService(memory, jobs, True, FakeTelemetry(), OperationSupervisor())
        assert await service.preferences_for("first") == ("old preference",)
        jobs.enqueue("first", "g", "a1", "old preference")
        jobs.enqueue("second", "g", "b1", "other preference")

        with pytest.raises(KeyError, match="memory not found"):
            await service.correct("first", "b", "wrong owner")
        with pytest.raises(KeyError, match="memory not found"):
            await service.delete_one("first", "missing")
        assert jobs.pending_count("first") == jobs.pending_count("second") == 1
        assert memory.rows["second"][0]["text"] == "other preference"
        for invalid in (" ", "x" * 181):
            with pytest.raises(ValueError):
                await service.correct("first", "a", invalid)
        assert jobs.pending_count("first") == 1

        updated = await service.correct("first", "a", "  corrected preference  ")
        assert updated["memories"][0]["text"] == "corrected preference"
        assert updated["memories"][0]["source"] == "user_correction"
        assert updated["queued_inputs"] == 0
        assert jobs.pending_count("second") == 1
        assert await service.preferences_for("first") == ("corrected preference",)

        service.enabled = False
        deleted = await service.delete_one("first", "a")
        assert deleted["memories"] == []
        assert memory.rows["second"][0]["text"] == "other preference"
        assert jobs.pending_count("second") == 1
        service.close()
    asyncio.run(run())


def test_inflight_read_cannot_restore_pre_correction_profile():
    async def run():
        memory, jobs = FakeMemory(), FakeJobs()
        jobs.set_enabled("player", True)
        memory.rows["player"] = [{"id": "a", "text": "old preference"}]
        captured, release = threading.Event(), threading.Event()
        original_list = memory.list_memories

        def held_snapshot(player_id):
            rows = [dict(row) for row in original_list(player_id)]
            if not captured.is_set():
                captured.set()
                assert release.wait(5)
            return rows

        memory.list_memories = held_snapshot
        service = PlayerMemoryService(memory, jobs, True, FakeTelemetry(), OperationSupervisor())
        read = asyncio.create_task(service.preferences_for("player"))
        try:
            assert await asyncio.to_thread(captured.wait, 5)
            assert (await service.correct("player", "a", "new preference"))["memories"][0]["text"] == "new preference"
        finally:
            release.set()
            assert await read == ()
        assert await service.preferences_for("player") == ("new preference",)
        service.close()
    asyncio.run(run())


def test_background_extraction_does_not_discard_new_turn_input():
    async def run():
        memory, jobs = FakeMemory(), FakeJobs()
        jobs.set_enabled("player", True)
        service = PlayerMemoryService(memory, jobs, True, FakeTelemetry(), OperationSupervisor())
        started_epoch = service.collection_epoch("player")
        batch = MemoryBatch("player", (("game", "old", "long enough prior input"),))
        jobs.batches.append(batch)
        assert await service._run_batch() == batch
        service.queue_input("player", "game", "new", "fresh long enough input",
                            expected_epoch=started_epoch)
        assert jobs.pending_count("player") == 1
        service.close()
    asyncio.run(run())
