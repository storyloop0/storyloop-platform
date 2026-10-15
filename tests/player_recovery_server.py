"""Real API and native SDK fixture for manual browser recovery acceptance."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from unittest.mock import patch

from openai import AsyncClient as OpenAIAsyncClient
import uvicorn

from player_recovery_process import ROOT, OfflineMemory, offline_transport, opening, settings
from storyloop_platform.bootstrap import build_portal
from storyloop_platform.portal.http_api import create_app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--posts", type=Path, required=True)
    parser.add_argument("--fault-marker", type=Path, required=True)
    parser.add_argument("--fault", choices=("after_snapshot", "before_snapshot"), default="after_snapshot")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    os.environ["STORY_MODEL_API_KEY"] = "offline-browser-key"
    os.environ["LANGFUSE_PUBLIC_KEY"] = ""
    os.environ["LANGFUSE_SECRET_KEY"] = ""
    transport = offline_transport(args.posts)
    original_init = OpenAIAsyncClient.__init__

    def sdk_init(client, *positional, **keywords):
        keywords["http_client"] = transport
        original_init(client, *positional, **keywords)

    with patch.object(OpenAIAsyncClient, "__init__", sdk_init):
        portal = build_portal(ROOT / "examples" / "catalog.json", settings(),
                              str(args.db), prologue_generator=opening)
        portal.memory_service.provider = OfflineMemory()
        portal.memory_service.enabled = True
        store = portal.turns.store
        original_save = store.save_generated_scene_snapshot

        def fail_once_after_save(snapshot):
            if args.fault == "before_snapshot" and not args.fault_marker.exists():
                args.fault_marker.write_text("scene not persisted", encoding="utf-8")
                raise RuntimeError("offline simulated scene save interruption")
            result = original_save(snapshot)
            if not args.fault_marker.exists():
                args.fault_marker.write_text("scene persisted", encoding="utf-8")
                raise RuntimeError("offline simulated response interruption")
            return result

        store.save_generated_scene_snapshot = fail_once_after_save
        uvicorn.run(create_app(portal), host="127.0.0.1", port=args.port,
                    log_level="warning")


if __name__ == "__main__":
    main()
