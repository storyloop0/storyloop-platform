"""Optional player profile collection, cached preferences, and worker lifecycle."""

from __future__ import annotations

import asyncio
import logging

from storyloop_harness.telemetry import Telemetry, session_id_for_game
from storyloop_platform.lifecycle import OperationSupervisor, finish_on_cancel
from storyloop_platform.memory.jobs import MemoryBatch, PlayerMemoryJobs
from storyloop_platform.memory.providers import PlayerMemory


class PlayerMemoryService:
    def __init__(self, provider: PlayerMemory | None, jobs: PlayerMemoryJobs,
                 enabled: bool, telemetry: Telemetry, operations: OperationSupervisor) -> None:
        self.operations = operations
        self._stop_requested = False
        self._worker_active = False
        self.provider = provider
        self.jobs = jobs
        self.enabled = enabled
        self.telemetry = telemetry
        self._profile_cache: dict[str, tuple[str, ...]] = {}
        self._profile_epochs: dict[str, int] = {}
        self._collection_epochs: dict[str, int] = {}
        self._memory_access = asyncio.Lock()
        self._memory_wakeup = asyncio.Event()

    def request_stop(self) -> None:
        self._stop_requested = True
        self._memory_wakeup.set()

    def close(self) -> None:
        if self._worker_active:
            raise RuntimeError("memory worker is active")
        self.operations.assert_drained()
        if self.provider is not None:
            self.provider.close()

    async def status(self, player_id: str) -> dict:
        available = self.enabled and self.provider is not None
        enabled = available and self.jobs.enabled(player_id)
        memories = (await self.operations.run_blocking(
                        lambda: self.provider.list_memories(player_id), name="memory-list")
                    if self.provider is not None else [])
        return {"available": available, "enabled": enabled, "memories": memories,
                "queued_inputs": self.jobs.pending_count(player_id),
                "batch_size": 6, "min_interval_hours": 12,
                "charged_points": 0}

    async def set_enabled(self, player_id: str, enabled: bool) -> dict:
        if enabled and not self.enabled:
            raise ValueError("玩家画像尚未由平台开放")
        async with self._memory_access:
            self.jobs.set_enabled(player_id, enabled)
            self._invalidate_profile(player_id)
            self._invalidate_collection(player_id)
        return await self.status(player_id)

    async def clear(self, player_id: str) -> dict:
        if self.provider is None:
            raise ValueError("玩家画像当前不可用")
        async with self._memory_access:
            self._invalidate_profile(player_id)
            self._invalidate_collection(player_id)
            self.jobs.clear(player_id)
            try:
                await self.operations.run_blocking(lambda: self.provider.clear(player_id), name="memory-clear")
            finally:
                # Blocking work settles before cancellation propagates. Invalidate
                # reads started during the clear even if cleanup failed afterward.
                self._invalidate_profile(player_id)
        return await self.status(player_id)

    async def correct(self, player_id: str, memory_id: str, text: str) -> dict:
        if self.provider is None:
            raise ValueError("玩家画像当前不可用")
        corrected = text.strip()
        if not 1 <= len(corrected) <= 180:
            raise ValueError("偏好内容须为 1 到 180 个字符")
        async with self._memory_access:
            self._invalidate_profile(player_id)
            try:
                await self.operations.run_blocking(
                    lambda: self.provider.correct(player_id, memory_id, corrected),
                    name="memory-correct")
                self._invalidate_collection(player_id)
                self.jobs.clear(player_id)
            finally:
                self._invalidate_profile(player_id)
        return await self.status(player_id)

    async def delete_one(self, player_id: str, memory_id: str) -> dict:
        if self.provider is None:
            raise ValueError("玩家画像当前不可用")
        async with self._memory_access:
            self._invalidate_profile(player_id)
            try:
                await self.operations.run_blocking(
                    lambda: self.provider.delete_one(player_id, memory_id),
                    name="memory-delete-one")
                self._invalidate_collection(player_id)
                self.jobs.clear(player_id)
            finally:
                self._invalidate_profile(player_id)
        return await self.status(player_id)

    def _invalidate_profile(self, player_id: str) -> None:
        self._profile_epochs[player_id] = self._profile_epochs.get(player_id, 0) + 1
        self._profile_cache.pop(player_id, None)

    def _invalidate_collection(self, player_id: str) -> None:
        self._collection_epochs[player_id] = self.collection_epoch(player_id) + 1

    def collection_epoch(self, player_id: str) -> int:
        return self._collection_epochs.get(player_id, 0)

    async def preferences_for(self, player_id: str) -> tuple[str, ...]:
        if not self.enabled or self.provider is None or not self.jobs.enabled(player_id):
            return ()
        if player_id in self._profile_cache:
            return self._profile_cache[player_id]
        epoch = self._profile_epochs.get(player_id, 0)
        try:
            rows = await self.operations.run_blocking(
                lambda: self.provider.list_memories(player_id), name="memory-list")
            result = tuple(row["text"][:180] for row in rows[:8])
            if (epoch != self._profile_epochs.get(player_id, 0)
                    or not self.enabled or not self.jobs.enabled(player_id)):
                return ()
            self._profile_cache[player_id] = result
            return result
        except Exception:
            logging.getLogger(__name__).exception("player profile read failed")
            return ()

    def queue_input(self, player_id: str, game_id: str,
                    request_id: str, player_text: str,
                    *, expected_epoch: int | None = None) -> None:
        if not self.enabled or self.provider is None:
            return
        if expected_epoch is not None and expected_epoch != self.collection_epoch(player_id):
            return
        if player_text == "/continue" or player_text in {"/next", "/rest"} or player_text.startswith("/choose "):
            return
        try:
            if self.jobs.enqueue(player_id, game_id, request_id, player_text):
                self._memory_wakeup.set()
        except Exception:
            logging.getLogger(__name__).exception("player profile enqueue failed")

    async def run_worker(self) -> None:
        if not self.enabled or self.provider is None:
            return
        self._worker_active = True
        try:
            while not self._stop_requested:
                self._memory_wakeup.clear()
                task = asyncio.create_task(self._run_batch(), name="memory-batch")
                try:
                    batch = await finish_on_cancel(task)
                except asyncio.CancelledError:
                    self.request_stop()
                    # The protected batch has settled. Surface unexpected service
                    # failure to its owner before propagating normal cancellation.
                    task.result()
                    raise
                if batch is None and not self._stop_requested:
                    try:
                        await asyncio.wait_for(self._memory_wakeup.wait(), timeout=20)
                    except asyncio.TimeoutError:
                        pass
        finally:
            self._worker_active = False

    async def _run_batch(self) -> MemoryBatch | None:
        async with self._memory_access:
            batch = await self.operations.run_blocking(self.jobs.claim, name="memory-claim")
            if batch is None:
                return None
            with self.telemetry.span(
                "player-memory-batch", {"input_count": len(batch.items),
                                        "provider": "mem0", "billed_to_player": False},
                session_id=session_id_for_game(batch.items[0][0]),
            ) as span:
                try:
                    await self.operations.run_blocking(
                        lambda: self.provider.remember(batch.player_id,
                                                       tuple(item[2] for item in batch.items)),
                        name="memory-remember")
                except Exception:
                    span.metric("story.player_memory_batch_success", 0.0)
                    logging.getLogger(__name__).exception("player profile extraction failed")
                    await self.operations.run_blocking(lambda: self.jobs.fail(batch), name="memory-fail")
                else:
                    await self.operations.run_blocking(lambda: self.jobs.complete(batch), name="memory-complete")
                    span.metric("story.player_memory_batch_success", 1.0)
                finally:
                    # Extraction may partially persist before raising, and job
                    # settlement can fail after a successful provider write.
                    self._invalidate_profile(batch.player_id)
            return batch
