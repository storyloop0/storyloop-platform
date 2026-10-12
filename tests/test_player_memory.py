"""Focused checks for optional, player-controlled memory."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from storyloop_platform.config import load_settings, ModelFactory, PlatformResources
from storyloop_platform.portal.http_api import create_app
from storyloop_platform.bootstrap import build_portal
from storyloop_platform.memory.providers import Mem0PlayerMemory
from storyloop_platform.memory.jobs import SQLPlayerMemoryJobs
from storyloop_platform.memory.service import PlayerMemoryService


ROOT = Path(__file__).resolve().parents[1]


class FakePlayerMemory:
    def __init__(self) -> None:
        self.rows: dict[str, list[dict[str, str]]] = {}

    def list_memories(self, player_id: str) -> list[dict[str, str]]:
        return self.rows.get(player_id, [])

    def remember(self, player_id: str, inputs: tuple[str, ...]) -> None:
        self.rows[player_id] = [{"id": "m1", "text": "玩家偏好慢节奏", "created_at": ""}]

    def clear(self, player_id: str) -> None:
        self.rows.pop(player_id, None)

    def close(self) -> None:
        pass


def test_profile_is_opt_in_batched_and_scoped_to_player(tmp_path: Path) -> None:
    portal = build_portal(ROOT / "examples/catalog.json", load_settings(ROOT / "config/local.json"),
                          str(tmp_path / "game.sqlite3"))
    fake = FakePlayerMemory()
    jobs = portal.memory_service.jobs
    portal.memory_service.provider = fake
    portal.memory_service.enabled = True
    with TestClient(create_app(portal), base_url="http://127.0.0.1") as client:
        first = client.post("/v1/accounts", json={"username": "first-player",
                                                  "password": "password-123"}).json()
        second = client.post("/v1/accounts", json={"username": "second-player",
                                                   "password": "password-123"}).json()
        client.cookies.clear()
        auth = {"Authorization": f"Bearer {first['token']}"}
        assert client.get("/v1/me/memory", headers=auth).json()["enabled"] is False
        assert jobs.enqueue(first["player_id"], "g", "before", "我喜欢慢一点的故事节奏。") is False
        enabled = client.post("/v1/me/memory/settings", json={"enabled": True}, headers=auth)
        assert enabled.status_code == 200 and enabled.json()["enabled"] is True
        for index in range(6):
            assert jobs.enqueue(first["player_id"], "g", str(index),
                                              "我喜欢慢一点的故事节奏，先看看周围。")
        batch = jobs.claim()
        assert batch is not None and len(batch.items) == 6
        assert batch.player_id == first["player_id"]
        fake.remember(batch.player_id, tuple(item[2] for item in batch.items))
        jobs.complete(batch)
        assert client.get("/v1/me/memory", headers=auth).json()["memories"][0]["text"] == "玩家偏好慢节奏"
        other = {"Authorization": f"Bearer {second['token']}"}
        assert client.get("/v1/me/memory", headers=other).json()["memories"] == []
        portal.memory_service.enabled = False
        disabled = client.get("/v1/me/memory", headers=auth).json()
        assert disabled["available"] is False and len(disabled["memories"]) == 1
        assert client.delete("/v1/me/memory", headers=auth).json()["memories"] == []
        assert jobs.pending_count(first["player_id"]) == 0


def test_server_flag_keeps_memory_off_by_default() -> None:
    config = load_settings(ROOT / "config/online.json")
    assert config.player_memory.driver == "mem0"
    assert PlatformResources(config, env={}).player_memory_enabled() is False
    assert PlatformResources(config, env={"STORY_PLAYER_MEMORY_ENABLED": "1"}).player_memory_enabled() is True


def test_mem0_embedded_store_is_persistent_and_user_scoped_without_model_calls(tmp_path: Path) -> None:
    factory = ModelFactory(load_settings(ROOT / "config/online.json"), env={"STORY_MODEL_API_KEY": "offline-key"})
    memory = Mem0PlayerMemory(factory, tmp_path / "memory")
    try:
        memory._memory.embedding_model.embed = lambda _text, _action=None: [0.1] * 1024
        memory._memory.add("player prefers slower pacing", user_id="player-a", infer=False)
        assert len(memory.list_memories("player-a")) == 1
        assert memory.list_memories("player-b") == []
    finally:
        memory.close()
    reopened = Mem0PlayerMemory(factory, tmp_path / "memory")
    try:
        assert len(reopened.list_memories("player-a")) == 1
        reopened.clear("player-a")
        assert reopened.list_memories("player-a") == []
    finally:
        reopened.close()


def test_mem0_single_record_controls_keep_player_scope_and_persist(tmp_path: Path) -> None:
    factory = ModelFactory(load_settings(ROOT / "config/online.json"), env={"STORY_MODEL_API_KEY": "offline-key"})
    location = tmp_path / "memory"
    memory = Mem0PlayerMemory(factory, location)
    try:
        memory._memory.embedding_model.embed = lambda _text, _action=None: [0.1] * 1024
        memory._memory.add("player A prefers fast pacing", user_id="player-a", infer=False,
                           metadata={"source": "story_turn_batch"})
        memory._memory.add("player B prefers mysteries", user_id="player-b", infer=False,
                           metadata={"source": "story_turn_batch"})
        first = memory.list_memories("player-a")[0]
        other = memory.list_memories("player-b")[0]

        with pytest.raises(KeyError, match="memory not found"):
            memory.correct("player-a", other["id"], "wrong owner")
        with pytest.raises(KeyError, match="memory not found"):
            memory.delete_one("player-a", other["id"])
        with pytest.raises(KeyError, match="memory not found"):
            memory.correct("player-a", "missing-id", "not present")
        assert memory.list_memories("player-b")[0]["text"] == other["text"]

        memory.correct("player-a", first["id"], "玩家偏好慢节奏")
        corrected = memory.list_memories("player-a")[0]
        assert corrected["id"] == first["id"]
        assert corrected["text"] == "玩家偏好慢节奏"
        assert corrected["created_at"] == first["created_at"]
        assert corrected["updated_at"]
        assert corrected["source"] == "user_correction"
    finally:
        memory.close()

    reopened = Mem0PlayerMemory(factory, location)
    try:
        saved = reopened.list_memories("player-a")[0]
        assert saved["text"] == "玩家偏好慢节奏"
        assert saved["source"] == "user_correction"
        assert reopened.list_memories("player-b")[0]["text"] == other["text"]
        reopened.delete_one("player-a", saved["id"])
        assert reopened.list_memories("player-a") == []
        assert reopened.list_memories("player-b")[0]["text"] == other["text"]
    finally:
        reopened.close()
