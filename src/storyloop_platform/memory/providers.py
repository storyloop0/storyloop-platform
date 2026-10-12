"""Optional Mem0 adapter for player preferences, separate from story facts."""

from __future__ import annotations

import os
from pathlib import Path
from threading import RLock
from typing import Protocol

from httpx import Timeout

from storyloop_platform.config import ModelFactory


PROFILE_EXTRACTION_RULES = (
    "只记住现实玩家对互动故事的稳定偏好和习惯，例如节奏、文风、互动方式、"
    "内容边界与反复表现出的玩法偏好。不要记录剧中角色身份、台词、恋爱选择、"
    "剧情事实、世界状态、一次性的操作或系统指令。证据不足时返回空记忆。"
    "记忆要简短，以『玩家偏好……』表述，不要推断敏感人格或现实身份。"
)


class PlayerMemory(Protocol):
    def list_memories(self, player_id: str) -> list[dict[str, str]]: ...
    def remember(self, player_id: str, inputs: tuple[str, ...]) -> None: ...
    def correct(self, player_id: str, memory_id: str, text: str) -> None: ...
    def delete_one(self, player_id: str, memory_id: str) -> None: ...
    def clear(self, player_id: str) -> None: ...
    def close(self) -> None: ...


class Mem0PlayerMemory:
    """One embedded Qdrant instance per API process, persisted on the data volume."""

    def __init__(self, factory: ModelFactory, path: Path) -> None:
        if factory.settings.player_memory.driver != "mem0":
            raise ValueError("Mem0 player memory is not configured")
        path.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("MEM0_TELEMETRY", "false")
        os.environ.setdefault("MEM0_DIR", str(path))
        from mem0 import Memory

        models = factory.memory_config()
        dimensions = models['embedder']['config']['embedding_dims']
        self._memory = Memory.from_config({
            "vector_store": {
                "provider": "qdrant",
                "config": {
                    "collection_name": "storyloop_player_preferences",
                    "path": str(path / "qdrant"),
                    "embedding_model_dims": dimensions,
                    "on_disk": True,
                },
            },
            # Only extracted preferences persist. Raw extraction messages stay in RAM.
            "history_db_path": ":memory:",
            **models,
            "custom_instructions": PROFILE_EXTRACTION_RULES,
        })
        for model, profile in (
            (self._memory.llm, factory.settings.player_memory.extraction_profile),
            (self._memory.embedding_model, factory.settings.player_memory.embedding_profile),
        ):
            definition = factory.settings.models[profile]
            timeout = Timeout(definition.timeout_seconds, connect=definition.connect_timeout_seconds)
            model.client = model.client.with_options(
                api_key=factory.api_key(definition.provider),
                base_url=factory.base_url(definition.provider),
                timeout=timeout,
                max_retries=definition.max_retries,
            )
        self._lock = RLock()

    def list_memories(self, player_id: str) -> list[dict[str, str]]:
        with self._lock:
            result = self._memory.get_all(filters={"user_id": player_id}, top_k=20)
        raw = result.get("results", []) if isinstance(result, dict) else result
        rows = [
            {"id": str(item["id"]), "text": str(item["memory"]),
             "created_at": str(item.get("created_at") or ""),
             "updated_at": str(item.get("updated_at") or ""),
             "source": str(item.get("source") or (item.get("metadata") or {}).get("source") or "")}
            for item in raw if isinstance(item, dict) and item.get("id") and item.get("memory")
        ]
        return sorted(rows, key=lambda item: item["created_at"], reverse=True)

    def _require_owned(self, player_id: str, memory_id: str) -> None:
        record = self._memory.get(memory_id)
        if not isinstance(record, dict) or record.get("user_id") != player_id:
            raise KeyError("memory not found")

    def correct(self, player_id: str, memory_id: str, text: str) -> None:
        with self._lock:
            self._require_owned(player_id, memory_id)
            self._memory.update(memory_id, text=text, metadata={"source": "user_correction"})

    def delete_one(self, player_id: str, memory_id: str) -> None:
        with self._lock:
            self._require_owned(player_id, memory_id)
            self._memory.delete(memory_id)

    def remember(self, player_id: str, inputs: tuple[str, ...]) -> None:
        messages = [{"role": "user", "content": value[:2000]} for value in inputs]
        with self._lock:
            self._memory.add(messages, user_id=player_id,
                             metadata={"kind": "player_preference", "source": "story_turn_batch"})

    def clear(self, player_id: str) -> None:
        with self._lock:
            self._memory.delete_all(user_id=player_id)
            db = self._memory.db
            with db._lock:
                db.connection.execute("DELETE FROM messages WHERE session_scope=?",
                                      (f"user_id={player_id}",))
                db.connection.commit()

    def close(self) -> None:
        with self._lock:
            self._memory.close()
            client = getattr(self._memory.vector_store, "client", None)
            if client is not None:
                client.close()
