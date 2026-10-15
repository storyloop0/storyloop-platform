"""Child process for durable player recovery acceptance tests.

The only injected model behavior is an offline HTTP transport.  The portal,
runtime factory, OpenAI adapter, game store, and settlement remain real.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from unittest.mock import patch

from httpx2 import AsyncClient as HttpAsyncClient, MockTransport, Response
from openai import AsyncClient as OpenAIAsyncClient
from storyloop_harness.advanced import (GeneratedSceneSnapshot, Observation,
                                        PendingWork, WorldEvent)

from storyloop_platform.bootstrap import build_portal
from storyloop_platform.config import PlatformSettings, default_settings


ROOT = Path(__file__).resolve().parents[1]
TEXT = "I ask the dockhand about the morning boat."
REQUEST_ID = "process-recovery"


class OfflineMemory:
    """No embedding or network access; SQL still owns collection/queue state."""

    def list_memories(self, _player_id):
        return []

    def correct(self, _player_id, _memory_id, _text):
        return None

    def close(self):
        return None


async def opening(*_args):
    return "A quiet harbor morning."


def settings(price: str = "2", billed: bool = True) -> PlatformSettings:
    raw = default_settings("local").model_dump(mode="json")
    raw["providers"]["default"]["base_url"] = "https://offline.invalid/v1"
    raw["models"]["story"]["model"] = "offline-scene"
    raw["models"]["story"]["max_retries"] = 0
    raw["models"]["story"]["rate"] = {
        "pricing_version": "original-price", "input_rmb_per_million": price,
        "output_rmb_per_million": "4", "cached_input_rmb_per_million": "1",
        "multiplier": "1",
    }
    if not billed:
        raw["credits"] = None
    return PlatformSettings.model_validate(raw)


def append_post(path: Path) -> None:
    with path.open("ab", buffering=0) as stream:
        stream.write(b"POST\n")
        os.fsync(stream.fileno())


def offline_transport(post_log: Path, *, exit_during_request: bool = False):
    def reply(request):
        assert request.method == "POST"
        assert request.url.path.endswith("/chat/completions")
        append_post(post_log)
        if exit_during_request:
            os._exit(73)
        decision = {
            "prose": "The dockhand lowers the rope and turns toward me.",
            "participants": ["dockhand"],
            "replies": [{"actor_id": "dockhand", "speech": "The morning boat is due soon."}],
            "delivery": "targets", "encounters": [],
        }
        return Response(200, json={
            "id": "chatcmpl-process-fixture", "created": 0,
            "model": "offline-scene", "object": "chat.completion",
            "usage": {
                "prompt_tokens": 1000, "completion_tokens": 500,
                "total_tokens": 1500,
                "prompt_tokens_details": {"cached_tokens": 200},
            },
            "choices": [{"finish_reason": "tool_calls", "index": 0,
                         "message": {"role": "assistant", "content": None,
                                     "tool_calls": [{"id": "scene-1", "type": "function",
                                                     "function": {"name": "generate_structured_output",
                                                                  "arguments": json.dumps(decision)}}]}}],
        })
    return HttpAsyncClient(transport=MockTransport(reply))


def install_boundary(portal, boundary: str, *, saved_usage_missing: bool = False) -> None:
    store = portal.turns.store
    if boundary == "after_marker":
        original = store.create_generated_scene_generation_marker

        def exit_after_marker(marker):
            result = original(marker)
            if result is True:
                os._exit(73)
            return result

        store.create_generated_scene_generation_marker = exit_after_marker
    elif boundary == "before_snapshot":
        def exit_before_snapshot(snapshot):
            os._exit(73)

        store.save_generated_scene_snapshot = exit_before_snapshot
    elif boundary == "after_snapshot":
        original = store.save_generated_scene_snapshot

        def exit_after_snapshot(snapshot):
            if saved_usage_missing:
                metadata = snapshot.metadata()
                for key in ("snapshot_id", "scene", "success_usage", "usage_coverage",
                            "format_version", "generation_contract_version"):
                    metadata.pop(key)
                snapshot = GeneratedSceneSnapshot.create(
                    scene=snapshot.scene(), success_usage=(), usage_coverage="missing",
                    **metadata)
            result = original(snapshot)
            os._exit(73)
            return result

        store.save_generated_scene_snapshot = exit_after_snapshot
    elif boundary in {"after_input", "after_timed_cue"}:
        original = store.commit

        def exit_after_input(game_id, expected_version, event, *args, **kwargs):
            result = original(game_id, expected_version, event, *args, **kwargs)
            target = (f"portal-{REQUEST_ID}:input" if boundary == "after_input"
                      else "morning-bell:occurred")
            if event is not None and event.event_id == target:
                os._exit(73)
            return result

        store.commit = exit_after_input


def install_exception_fault(portal, fault: str) -> None:
    def interrupted(*_args, **_kwargs):
        raise RuntimeError(f"offline {fault} interruption")

    if fault == "npc_work":
        store = portal.turns.store
        original = store.commit

        def fail_before_npc(game_id, expected_version, event, *args, **kwargs):
            if event is not None and ":reply:dockhand:spoken" in event.event_id:
                interrupted()
            return original(game_id, expected_version, event, *args, **kwargs)

        store.commit = fail_before_npc
    elif fault == "guidance":
        from storyloop_platform.runtime.guidance import GuidanceAdvisor
        GuidanceAdvisor.advise = interrupted
    elif fault == "view":
        from storyloop_platform.gameplay import turns
        turns.turn_view = interrupted
    elif fault == "prepare":
        portal.turns.settlements.prepare = interrupted
    elif fault == "settle_after_commit":
        original = portal.billing.settle_turn

        def settle_then_lose_ack(*args, **kwargs):
            original(*args, **kwargs)
            interrupted()

        portal.billing.settle_turn = settle_then_lose_ack


async def run(args) -> None:
    data = json.loads(args.state.read_text(encoding="utf-8")) if args.state.exists() else {}
    transport = offline_transport(
        args.posts, exit_during_request=args.mode == "crash" and args.boundary == "during_request")
    original_init = OpenAIAsyncClient.__init__

    def sdk_init(client, *positional, **keywords):
        if args.mode in {"resume", "status"}:
            raise AssertionError("recovery constructed an OpenAI client")
        keywords["http_client"] = transport
        original_init(client, *positional, **keywords)

    with patch.object(OpenAIAsyncClient, "__init__", sdk_init):
        portal = build_portal(ROOT / "examples" / "catalog.json",
                              settings(args.price, not args.no_billing), str(args.db),
                              prologue_generator=opening)
        portal.memory_service.provider = OfflineMemory()
        portal.memory_service.enabled = True
        try:
            if args.mode == "seed":
                player = portal.register("process-player", "password-123")
                await portal.set_memory_enabled(player["token"], True)
                game = await portal.create_save(player["token"], "npc-chat")
                game_id = game["game_id"]
                before = portal.gameplay.store.load(game_id)
                old = WorldEvent("old-visible-event", "old_scene", None, None,
                                 before.tick, (), {"summary": "OLD OBSERVATION EXCLUDED"})
                observation = Observation("old-visible-observation", old.event_id,
                                          "player", "witnessed", "OLD OBSERVATION EXCLUDED",
                                          before.tick)
                cue = PendingWork("morning-bell", "scenario_cue", 1, 1, None, {
                    "event_kind": "morning_bell", "summary": "TIMED CUE RETAINED",
                    "location": "harbor_square", "sensory": "TIMED CUE RETAINED",
                    "effects": [{"path": ["plot", "pressure"], "value": 1}],
                })
                portal.gameplay.store.commit(game_id, before.version, old,
                                             (observation,), (cue,))
                data = {"game_id": game["game_id"], "token": player["token"],
                        "player_id": player["player_id"]}
                args.state.write_text(json.dumps(data), encoding="utf-8")
                print(json.dumps(data), flush=True)
                return
            if args.mode == "memory_change":
                if args.memory_change == "disable":
                    await portal.set_memory_enabled(data["token"], False)
                else:
                    await portal.correct_memory(data["token"], "offline-memory",
                                                "I prefer accurate recovery.")
                print(json.dumps({"queued_inputs": portal.memory_service.jobs.pending_count(
                    data["player_id"])}), flush=True)
                return
            if args.mode == "status":
                import hashlib
                digest = hashlib.sha256(TEXT.encode("utf-8")).hexdigest()
                result = portal.turn_recovery_status(data["token"], data["game_id"],
                                                     REQUEST_ID, digest)
                print(json.dumps({"status": result,
                                  "ledger": portal.credit_ledger(data["token"])}), flush=True)
                return
            if args.mode == "crash":
                if args.fault:
                    install_exception_fault(portal, args.fault)
                else:
                    install_boundary(portal, args.boundary,
                                     saved_usage_missing=args.saved_usage_missing)
            if args.mode == "resume":
                from storyloop_platform.portal.turn_errors import TurnGenerationUncertain, turn_failure
                try:
                    result = await portal.turn(data["token"], data["game_id"], TEXT, REQUEST_ID)
                except TurnGenerationUncertain as error:
                    print(json.dumps({"failure": turn_failure(error, REQUEST_ID)}), flush=True)
                    return
            else:
                result = await portal.turn(data["token"], data["game_id"], TEXT, REQUEST_ID)
            print(json.dumps({"view": result,
                              "ledger": (portal.credit_ledger(data["token"])
                                         if portal.billing is not None else []),
                              "history": portal.history(data["token"], data["game_id"]),
                              "queued_inputs": portal.memory_service.jobs.pending_count(
                                  data["player_id"])}), flush=True)
        finally:
            portal.close()
            await transport.aclose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("seed", "crash", "resume", "memory_change",
                                         "status"))
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--posts", type=Path, required=True)
    parser.add_argument("--boundary", choices=("after_marker", "during_request",
                                               "before_snapshot", "after_snapshot", "after_input",
                                               "after_timed_cue"),
                        default="after_snapshot")
    parser.add_argument("--price", default="2")
    parser.add_argument("--no-billing", action="store_true")
    parser.add_argument("--fault", choices=("npc_work", "guidance", "view",
                                            "prepare", "settle_after_commit"))
    parser.add_argument("--memory-change", choices=("disable", "correct"),
                        default="disable")
    parser.add_argument("--saved-usage-missing", action="store_true")
    args = parser.parse_args()
    if args.mode in {"resume", "memory_change", "status"}:
        os.environ.pop("STORY_MODEL_API_KEY", None)
    else:
        os.environ["STORY_MODEL_API_KEY"] = "offline-key"
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
