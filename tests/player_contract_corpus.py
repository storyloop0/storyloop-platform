"""Explicit offline Python -> production Node contract gate, outside pytest discovery."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
from unittest.mock import call, patch
import zipfile

from fastapi.testclient import TestClient
from httpx2 import Request
from openai import APIConnectionError
from openai import AsyncClient as OpenAIAsyncClient
from storyloop_harness.testing import OfflineModel
from storyloop_harness.usage import collect_usage, record_model_usage
from storyloop_platform.bootstrap import build_portal
from storyloop_platform.config import ModelFactory, default_settings, load_settings
from storyloop_platform.gameplay.factory import RuntimeFactory
from storyloop_platform.portal.http_api import create_app
from storyloop_platform.runtime.campaign import CampaignProgram

from committed_input import commit_player_input
from player_recovery_process import OfflineMemory, offline_transport, opening as native_opening, settings as native_settings

ROOT = Path(__file__).resolve().parents[1]


class PricedOfflineModel(OfflineModel):
    async def __call__(self, *args, **kwargs):
        messages = args[0]
        content = messages[-1]['content']
        if isinstance(content, list):
            content = ''.join(part['text'] for part in content if part.get('type') == 'text')
        request = json.loads(content)
        if 'source_story_milestones' in request:
            result = SimpleNamespace(metadata={
                "beats": [
                    {"kind": "narration", "text": "The visitor reaches the harbor. A worker pauses by the dock and waves."},
                    {"kind": "dialogue", "actor_id": "dockhand", "text": "Welcome to the harbor."},
                    {"kind": "narration", "text": "After a quiet conversation, the visitor says goodbye and walks toward the market."}],
                "participants": ["dockhand"], "delivery": "targets", "encounters": [],
                "milestones_fulfilled": ["ending"], "options": [
                    {"label": "Look around", "input": "I look around the harbor."},
                    {"label": "Read notice", "input": "I read the notice."},
                    {"label": "Say goodbye", "input": "I say goodbye."}],
            })
        else:
            with collect_usage():
                result = await super().__call__(*args, **kwargs)
        record_model_usage("offline", "single_turn", SimpleNamespace(input_tokens=1000, output_tokens=500))
        return result


class OfflineModelFactory(ModelFactory):
    def __init__(self, settings, **kwargs):
        super().__init__(settings, env={"CONTRACT_MODEL_KEY": "offline-synthetic"}, **kwargs)
        self.model = PricedOfflineModel()

    def create_model(self, task, *, temperature=None):
        assert self.settings.model_for(task).model == "offline"
        return self.model


class OfflinePresenter:
    async def present(self, context):
        record_model_usage("offline", "narration", SimpleNamespace(input_tokens=20, output_tokens=10))
        return "The harbor settles into the afternoon."


class OfflineRuntimeFactory(RuntimeFactory):
    def novel_presenter(self, package, game_id):
        return OfflinePresenter()


def _write(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def _packages(root: Path) -> Path:
    for name in ("freeform", "campaign", "source"):
        shutil.copytree(ROOT / "examples/freeform", root / name)
        manifest_path = root / name / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["initial_state"]["contract_status"] = {"flag": False, "count": 0, "text": "0"}
        manifest["status_fields"] = [
            {"id": field, "label": field.title(), "path": ["contract_status", field],
             **({"bounds": {"min": 0, "max": 10, "max_delta": 1}} if name == "freeform" and field == "count" else {})}
            for field in ("flag", "count", "text")]
        if name == "freeform":
            manifest["facts"] = [{"id": "harbor.answer", "text": "The harbor bell rings at noon.",
                                  "known_by": ["dockhand"], "disclose_to": ["player"]}]
        _write(manifest_path, manifest)
    program = {"id": "harbor-freeform", "ticks_per_day": 4, "final_tick": 4, "steps": [
        {"id": "arrival", "at": 0, "kind": "scene", "text": "A quiet harbor."},
        {"id": "choice", "at": 3, "kind": "choice", "prompt": "Who?",
         "options": [{"id": "dockhand", "label": "Dockhand"}]},
        {"id": "ending", "at": 4, "kind": "finale", "choice_key": "choice", "threshold": 0,
         "success_text": "Done", "other_text": "Done"}]}
    manifest_path = root / "campaign/manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["initial_state"]["campaign"] = CampaignProgram.from_dict(program).initial_state(["dockhand"])
    manifest["authored_prologue"] = "A quiet harbor."
    _write(manifest_path, manifest)
    _write(root / "campaign/campaign.json", program)
    manifest_path = root / "source/manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["story_blueprint"] = "story_blueprint.json"
    manifest["authored_prologue"] = "{{player_intro}}\n\nA traveler reaches the harbor and pauses by the dock."
    manifest["actors"][0]["public_profile"] = "A harbor worker wearing an old coat, sorting ropes by the dock."
    worldbook_path = root / "source" / manifest["worldbook"]
    worldbook = json.loads(worldbook_path.read_text(encoding="utf-8"))
    for entry in worldbook["entries"]:
        if entry["id"] == manifest["actors"][0]["card"]:
            entry["text"] = ("The worker has spent years at the harbor and knows the daily boat schedules. "
                             "They greet visitors cautiously, ask where they are going, and describe only what "
                             "they have personally seen. They remember their experiences and do not know "
                             "secrets that nobody has told them. They like to keep the dock tidy and safe.")
    _write(worldbook_path, worldbook)
    _write(manifest_path, manifest)
    _write(root / "source/story_blueprint.json", {
        "source_document": "Synthetic harbor source",
        "opening_focus": "A new visitor reaches a harbor and pauses before speaking.",
        "setup": {"player_options": [{"id": "random", "label": "Visitor", "guidance": "A visitor",
                                       "source_ref": "Opening"}],
                  "tone_options": [{"id": "slow", "label": "Slow", "guidance": "Patient pacing",
                                     "source_ref": "Tone"}]},
        "actor_slots": [{"actor_id": "dockhand", "brief": "A friendly harbor worker.", "source_ref": "Cast"}],
        "player_profiles": [{"name": "Visitor", "appearance": "Carrying a bag"}],
        "opening_options": [{"label": "Say hello", "input": "I greet the worker."},
                            {"label": "Look around", "input": "I look at the boats."},
                            {"label": "Read notice", "input": "I read the notice."}],
        "facts": [{"id": "harbor", "text": "Visitors reach the harbor by boat.", "source_ref": "World",
                   "visibility": "public"}],
        "milestones": [{"id": "ending", "day": 1, "cue": "The visitor may leave after a conversation.",
                        "source_ref": "Ending", "terminal": True}],
    })
    catalog = root / "catalog.json"
    _write(catalog, {"games": [dict(id=name, title=name.title(), mode="freeform" if name == "freeform" else "campaign",
                                   play_modes=["campaign", "freeform"] if name != "freeform" else ["freeform"],
                                   package=name) for name in ("freeform", "campaign", "source")]})
    return catalog


def export_corpus(root: Path) -> list[dict]:
    catalog = _packages(root)
    config = root / "config.json"
    _write(config, {
        "providers": {"offline": {"base_url": "https://offline.invalid/v1", "api_key_env": "CONTRACT_MODEL_KEY"}},
        "models": {"offline": {"kind": "chat", "provider": "offline", "model": "offline", "rate": {
            "pricing_version": "contracts-offline-1", "input_rmb_per_million": "0.8", "output_rmb_per_million": "2.7",
            "cached_input_rmb_per_million": "0.1", "multiplier": "1"}}},
        "routes": {task: "offline" for task in default_settings().routes},
        "storage": {"path": "world.sqlite3"},
    })
    corpus: list[dict] = []
    with patch.dict(os.environ, {"STORY_UPLOAD_DIR": str(root / "uploads"), "LANGFUSE_PUBLIC_KEY": "",
                                 "LANGFUSE_SECRET_KEY": ""}), patch("storyloop_platform.bootstrap.ModelFactory", OfflineModelFactory):
        async def opening(*args):
            return "A quiet synthetic opening."

        portal = build_portal(catalog, load_settings(config), prologue_generator=opening,
                              runtime_factory_builder=OfflineRuntimeFactory)
        try:
            with TestClient(create_app(portal), base_url="http://127.0.0.1") as client:
                token = portal.register("reader", "offline-password-123")["token"]
                headers = {"Authorization": f"Bearer {token}"}

                def response(name, kind, method, path, *, status=200, **kwargs):
                    result = client.request(method, path, headers=headers, **kwargs)
                    assert result.status_code == status, (name, result.status_code, result.text)
                    if kind == "TurnStreamEvent":
                        events = [json.loads(line[6:]) for line in result.text.splitlines() if line.startswith("data: ")]
                        assert events and events[-1]["type"] in {"complete", "error"}, name
                        corpus.extend(dict(name=f"{name}:{i}", kind=kind, payload=event) for i, event in enumerate(events))
                        return events
                    payload = result.json()
                    corpus.append(dict(name=name, kind=kind, payload=payload))
                    return payload

                def create(name, mode=None):
                    return response(f"create:{name}:{mode or 'default'}", "View", "POST", "/v1/saves", status=201,
                                    json={"catalog_id": name, **({"play_mode": mode} if mode else {})})["game_id"]

                game = create("freeform")
                base = f"/v1/saves/{game}"
                response("resume:intro", "View", "POST", base + "/resume")
                response("history:intro", "History", "GET", base + "/history")
                fact_game = create("freeform")
                fact_base = f"/v1/saves/{fact_game}"
                fact = response("fact:known", "View", "POST", fact_base + "/fact-queries",
                                json=dict(request_id="fact-contract", fact_id="harbor.answer",
                                          speaker_id="dockhand"))
                assert fact["segments"][0]["text"] == "The harbor bell rings at noon."
                turn = dict(text="Hello", request_id="http-turn")
                billed = response("turn:freeform", "View", "POST", base + "/turns", json=turn)
                replay = response("replay:freeform", "View", "POST", base + "/turns", json=turn)
                assert billed == replay
                fingerprint = hashlib.sha256(turn["text"].encode()).hexdigest()
                response("status:complete", "TurnRecoveryStatus", "GET",
                         base + "/turns/http-turn/recovery", params={"input_hash": fingerprint})
                response("status:mismatch", "TurnRecoveryStatus", "GET",
                         base + "/turns/http-turn/recovery", params={"input_hash": "0" * 64})
                response("status:no-snapshot", "TurnRecoveryStatus", "GET",
                         base + "/turns/new-request/recovery", params={"input_hash": fingerprint})
                assert len([row for row in portal.credit_ledger(token) if row["kind"] == "turn"]) == 1
                response("resume:billed", "View", "POST", base + "/resume")
                response("history:billed", "History", "GET", base + "/history")
                response("stream:freeform", "TurnStreamEvent", "POST", base + "/turns/stream",
                         json=dict(text="Hello again", request_id="stream-turn"))
                campaign = create("campaign")
                create("campaign", "freeform")
                for step in range(1, 4):
                    gate = response(f"turn:campaign:{step}", "View", "POST", f"/v1/saves/{campaign}/turns",
                                    json=dict(text="/next", request_id=f"campaign-next-{step}"))
                    if gate["interaction"] is not None:
                        break
                assert gate["interaction"] is not None
                for mode in ("campaign", "freeform"):
                    source = create("source", mode)
                    response(f"turn:source:{mode}", "View", "POST", f"/v1/saves/{source}/turns",
                             json=dict(text="I greet the worker.", request_id=f"source-{mode}"))

                archive = io.BytesIO()
                with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
                    for path in (root / "freeform").glob("*.json"):
                        bundle.write(path, path.name)
                draft = asyncio.run(portal.upload_scenario(token, "Preview story", "", archive.getvalue()))
                submission = portal.submit_scenario(token, draft["id"])
                admin = portal.register("reviewer", "offline-password-123")["token"]
                portal.access.bootstrap_admin("reviewer")
                reader_headers = headers
                headers = {"Authorization": f"Bearer {admin}"}
                preview = response("preview:create", "View", "POST",
                                   f"/v1/manage/submissions/{submission['submission_id']}/preview")
                preview_turn = response("preview:turn", "View", "POST", f"/v1/saves/{preview['game_id']}/turns",
                                        json=dict(text="Hello", request_id="preview-turn"))
                assert "billing" not in preview_turn
                headers = reader_headers

                with patch("storyloop_platform.lifecycle.logger") as input_log:
                    for stream in (False, True):
                        response(f"failure:input:{stream}", "TurnStreamEvent" if stream else "TurnFailure", "POST",
                                 base + "/turns" + ("/stream" if stream else ""), status=200 if stream else 400,
                                 json=dict(text="x" * 10001, request_id="oversized"))
                    assert input_log.error.call_count == 1
                    assert isinstance(input_log.error.call_args.kwargs["exc_info"], ValueError)
                recovery = create("freeform")
                commit_player_input(portal.gameplay.store, recovery, "portal-recovery:input", "Hello")
                response("status:legacy", "TurnRecoveryStatus", "GET",
                         f"/v1/saves/{recovery}/turns/recovery/recovery",
                         params={"input_hash": hashlib.sha256(b"Hello").hexdigest()})
                # Capture only this deliberate recovery fault and assert its log;
                # unexpected failures elsewhere still reach ordinary logging.
                with patch("storyloop_platform.lifecycle.logger") as recovery_log:
                    for stream in (False, True):
                        response(f"failure:recovery:{stream}", "TurnStreamEvent" if stream else "TurnFailure", "POST",
                                 f"/v1/saves/{recovery}/turns" + ("/stream" if stream else ""), status=200 if stream else 409,
                                 json=dict(text="Hello", request_id="recovery"))
                    assert recovery_log.error.call_count == 1
                    assert type(recovery_log.error.call_args.kwargs["exc_info"]).__name__ == "TurnRecoveryRequired"

                class UnavailableModel:
                    async def __call__(self, *args, **kwargs):
                        raise APIConnectionError(request=Request("POST", "https://offline.invalid/v1"))

                portal.gameplay.factory.models.model = UnavailableModel()
                # Fresh saves prevent already cached engines from retaining the healthy model.
                with patch("storyloop_platform.portal.http_api.logger") as model_log, \
                        patch("storyloop_platform.lifecycle.logger") as operation_log:
                    for stream in (False, True):
                        failed = create("freeform")
                        response(f"failure:model:{stream}", "TurnStreamEvent" if stream else "TurnFailure", "POST",
                                 f"/v1/saves/{failed}/turns" + ("/stream" if stream else ""), status=200 if stream else 503,
                                 json=dict(text="Hello", request_id="model-failed"))
                    assert model_log.warning.call_args_list == [
                        call("model unavailable during portal turn: %s", "APIConnectionError"),
                        call("model unavailable during streamed turn: %s", "APIConnectionError"),
                    ]
                    model_log.exception.assert_not_called()
                    model_log.error.assert_not_called()
                    assert operation_log.error.call_count == 1
                    assert isinstance(operation_log.error.call_args.kwargs["exc_info"], APIConnectionError)
                portal.operations.stop_admission()
                for stream in (False, True):
                    response(f"failure:stopping:{stream}", "TurnFailure", "POST",
                             base + "/turns" + ("/stream" if stream else ""), status=503,
                             json=dict(text="Hello", request_id="stopping"))
        finally:
            portal.close()
    corpus.extend(_native_uncertain_corpus(root))
    return corpus


def _native_uncertain_corpus(root: Path) -> list[dict]:
    """Exercise the protected native scene engine with real offline SDK HTTP."""
    transport = offline_transport(root / "native-sdk-posts.log")
    original_init = OpenAIAsyncClient.__init__

    def sdk_init(client, *positional, **keywords):
        keywords["http_client"] = transport
        original_init(client, *positional, **keywords)

    rows: list[dict] = []
    with patch.object(OpenAIAsyncClient, "__init__", sdk_init), patch.dict(os.environ, {
        "STORY_MODEL_API_KEY": "offline-contract-key", "LANGFUSE_PUBLIC_KEY": "", "LANGFUSE_SECRET_KEY": "",
    }):
        portal = build_portal(ROOT / "examples/catalog.json", native_settings(),
                              str(root / "native.sqlite3"), prologue_generator=native_opening)
        portal.memory_service.provider = OfflineMemory()
        portal.memory_service.enabled = True
        try:
            with TestClient(create_app(portal), base_url="http://127.0.0.1") as client:
                token = portal.register("native-reader", "offline-password-123")["token"]
                headers = {"Authorization": f"Bearer {token}"}
                for stream in (False, True):
                    game = asyncio.run(portal.create_save(token, "npc-chat"))["game_id"]
                    path = f"/v1/saves/{game}/turns"
                    store = portal.turns.store
                    original_save = store.save_generated_scene_snapshot

                    def fail_before_save(snapshot):
                        raise RuntimeError("offline native scene save interruption")

                    store.save_generated_scene_snapshot = fail_before_save
                    try:
                        with patch("storyloop_platform.lifecycle.logger"), patch("storyloop_platform.portal.http_api.logger"):
                            result = client.post(path + ("/stream" if stream else ""), headers=headers,
                                                 json={"text": "I ask the dockhand about the morning boat.",
                                                       "request_id": "native-uncertain"})
                    finally:
                        store.save_generated_scene_snapshot = original_save
                    assert result.status_code == (200 if stream else 409), result.text
                    if stream:
                        events = [json.loads(line[6:]) for line in result.text.splitlines() if line.startswith("data: ")]
                        rows.extend({"name": f"failure:uncertain:{stream}:{i}", "kind": "TurnStreamEvent",
                                     "payload": event} for i, event in enumerate(events))
                        assert events[-1]["code"] == "GENERATION_UNCERTAIN", events
                    else:
                        rows.append({"name": f"failure:uncertain:{stream}", "kind": "TurnFailure",
                                     "payload": result.json()})
                    status = client.get(path + "/native-uncertain/recovery", headers=headers,
                                        params={"input_hash": hashlib.sha256(
                                            b"I ask the dockhand about the morning boat.").hexdigest()})
                    assert status.status_code == 200, status.text
                    rows.append({"name": f"status:uncertain:{stream}", "kind": "TurnRecoveryStatus",
                                 "payload": status.json()})
                    assert status.json()["reason_code"] == "GENERATION_UNCERTAIN"
        finally:
            portal.close()
            asyncio.run(transport.aclose())
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", default="node")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="player-contracts-") as temporary:
        root = Path(temporary)
        corpus = root / "corpus.json"
        _write(corpus, {"responses": export_corpus(root)})
        subprocess.run([args.node, "--experimental-strip-types", str(ROOT / "web/scripts/check-contract-corpus.mjs"),
                        str(corpus)], check=True, cwd=ROOT / "web")


if __name__ == "__main__":
    main()
