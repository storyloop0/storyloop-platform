"""Focused checks for optional, player-controlled memory."""

from __future__ import annotations

from pathlib import Path
from datetime import datetime, timedelta, timezone
import asyncio

from fastapi.testclient import TestClient
import pytest

from storyloop_platform.config import load_settings, ModelFactory, PlatformResources
from storyloop_platform.portal.http_api import create_app
from storyloop_platform.bootstrap import build_portal
from storyloop_platform.memory.providers import Mem0PlayerMemory
from storyloop_platform.memory.jobs import SQLPlayerMemoryJobs
from storyloop_platform.memory.service import PlayerMemoryService
from storyloop_platform.lifecycle import OperationSupervisor


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

    def correct(self, player_id: str, memory_id: str, text: str) -> None:
        for row in self.rows.get(player_id, []):
            if row["id"] == memory_id:
                row["text"] = text
                row["source"] = "user_correction"
                return
        raise KeyError("memory not found")

    def delete_one(self, player_id: str, memory_id: str) -> None:
        rows = self.rows.get(player_id, [])
        if not any(row["id"] == memory_id for row in rows):
            raise KeyError("memory not found")
        self.rows[player_id] = [row for row in rows if row["id"] != memory_id]

    def close(self) -> None:
        pass


def test_profile_is_opt_in_batched_and_scoped_to_player(tmp_path: Path) -> None:
    portal = build_portal(ROOT / "examples/catalog.json", load_settings(ROOT / "config/local.json"),
                          str(tmp_path / "game.sqlite3"))
    fake = FakePlayerMemory()
    jobs = portal.memory_service.jobs
    portal.memory_service.provider = fake
    portal.memory_service.enabled = True
    portal.memory_service._utc_now = lambda: datetime(2026, 10, 14, tzinfo=timezone.utc)
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


def test_player_memory_http_single_record_controls_are_authenticated_and_scoped(tmp_path: Path) -> None:
    portal = build_portal(ROOT / "examples/catalog.json", load_settings(ROOT / "config/local.json"),
                          str(tmp_path / "game.sqlite3"))
    fake = FakePlayerMemory()
    portal.memory_service.provider = fake
    portal.memory_service.enabled = True
    portal.memory_service._utc_now = lambda: datetime(2026, 10, 14, tzinfo=timezone.utc)
    with TestClient(create_app(portal), base_url="http://127.0.0.1") as client:
        first = client.post("/v1/accounts", json={"username": "memory-first",
                                                   "password": "password-123"}).json()
        second = client.post("/v1/accounts", json={"username": "memory-second",
                                                    "password": "password-123"}).json()
        client.cookies.clear()
        auth = {"Authorization": f"Bearer {first['token']}"}
        other = {"Authorization": f"Bearer {second['token']}"}
        fake.rows[first["player_id"]] = [{"id": "own", "text": "old", "created_at": "2026-07-16T00:00:00Z",
                                          "updated_at": "2026-10-13T00:00:00Z", "source": "story_turn_batch"}]
        fake.rows[second["player_id"]] = [{"id": "foreign", "text": "other", "created_at": ""}]
        jobs = portal.memory_service.jobs
        jobs.set_enabled(first["player_id"], True)
        jobs.set_enabled(second["player_id"], True)
        assert client.get("/v1/me/memory").status_code == 401
        body = client.get("/v1/me/memory", headers=auth).json()
        assert body["generated_preference_max_age_days"] == 90
        assert body["memories"][0] == dict(fake.rows[first["player_id"]][0],
                                           freshness="expired", expires_at="2026-10-14T00:00:00Z")
        assert jobs.enqueue(first["player_id"], "g", "a1", "old preference input")
        assert jobs.enqueue(second["player_id"], "g", "b1", "other preference input")

        assert client.patch("/v1/me/memory/own", json={"text": "changed"}).status_code == 401
        foreign = client.patch("/v1/me/memory/foreign", json={"text": "wrong"}, headers=auth)
        missing = client.patch("/v1/me/memory/missing", json={"text": "wrong"}, headers=auth)
        assert foreign.status_code == missing.status_code == 404
        assert foreign.json() == missing.json()
        assert client.delete("/v1/me/memory/foreign", headers=auth).status_code == 404
        assert jobs.pending_count(first["player_id"]) == jobs.pending_count(second["player_id"]) == 1
        assert client.patch("/v1/me/memory/own", json={"text": "   "}, headers=auth).status_code in {400, 422}
        assert client.patch("/v1/me/memory/own", json={"text": "x" * 181}, headers=auth).status_code in {400, 422}
        assert jobs.pending_count(first["player_id"]) == 1

        corrected = client.patch("/v1/me/memory/own", json={"text": "  new preference  "}, headers=auth)
        assert corrected.status_code == 200
        assert corrected.json()["memories"][0]["text"] == "new preference"
        assert corrected.json()["memories"][0]["source"] == "user_correction"
        assert corrected.json()["memories"][0]["freshness"] == "current"
        assert corrected.json()["memories"][0]["expires_at"] is None
        assert corrected.json()["queued_inputs"] == 0
        assert jobs.pending_count(second["player_id"]) == 1
        portal.memory_service.enabled = False
        deleted = client.delete("/v1/me/memory/own", headers=auth)
        assert deleted.status_code == 200 and deleted.json()["memories"] == []
        assert client.get("/v1/me/memory", headers=other).json()["memories"][0]["text"] == "other"


def test_persisted_read_derives_expiry_without_model_or_mutator_calls(tmp_path: Path, monkeypatch) -> None:
    factory = ModelFactory(load_settings(ROOT / "config/online.json"), env={"STORY_MODEL_API_KEY": "offline-key"})
    location = tmp_path / "memory"
    memory = Mem0PlayerMemory(factory, location)
    try:
        memory._memory.embedding_model.embed = lambda _text, _action=None: [0.1] * 1024
        memory._memory.add("player prefers slower pacing", user_id="player-a", infer=False,
                           metadata={"source": "story_turn_batch"})
        seeded = memory.list_memories("player-a")[0]
    finally:
        memory.close()

    reopened = Mem0PlayerMemory(factory, location)
    def unexpected(*args, **kwargs):
        raise AssertionError("read-only freshness must not call models or mutators")
    try:
        for method in ("embed",):
            monkeypatch.setattr(reopened._memory.embedding_model, method, unexpected)
        for method in ("search", "add", "update", "delete", "delete_all"):
            monkeypatch.setattr(reopened._memory, method, unexpected)
        monkeypatch.setattr(reopened._memory.llm, "generate_response", unexpected)
        saved = reopened.list_memories("player-a")[0]
        assert (saved["id"], saved["text"], saved["created_at"]) == (
            seeded["id"], seeded["text"], seeded["created_at"])

        class Jobs:
            def enabled(self, _player_id):
                return True
            def pending_count(self, _player_id):
                return 0
        created = datetime.fromisoformat(saved["created_at"].replace("Z", "+00:00"))
        now = created.astimezone(timezone.utc) + timedelta(days=90)
        service = PlayerMemoryService(reopened, Jobs(), True, None, OperationSupervisor(),
                                      utc_now=lambda: now)
        async def read():
            assert await service.preferences_for("player-a") == ()
            status = await service.status("player-a")
            assert status["memories"][0]["freshness"] == "expired"
            assert status["memories"][0]["created_at"] == saved["created_at"]
            assert status["memories"][0]["id"] == saved["id"]
        asyncio.run(read())
    finally:
        reopened.close()
