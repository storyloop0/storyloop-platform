"""Local browser acceptance API with a deterministic model and real SQLite storage."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import uvicorn
from storyloop_harness.testing import OfflineModel

from storyloop_platform.bootstrap import build_portal
from storyloop_platform.config import default_settings, load_settings
from storyloop_platform.gameplay.factory import RuntimeFactory
from storyloop_platform.portal.http_api import create_app


ROOT = Path(__file__).resolve().parents[1]


class OfflineModels:
    def require_credentials(self) -> None:
        pass

    def create_model(self, task: str, *, temperature: float | None = None) -> OfflineModel:
        return OfflineModel()


def offline_runtime(**dependencies):
    dependencies["models"] = OfflineModels()
    return RuntimeFactory(**dependencies)


async def offline_prologue(*_args) -> str:
    return "港口的晨雾尚未散去。"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    local = default_settings("local")
    config = args.db.parent / "offline-config.json"
    config.write_text(json.dumps({
        "providers": {"offline": {"base_url": "https://offline.invalid/v1",
                                  "api_key_env": "STORY_MODEL_API_KEY"}},
        "models": {"offline": {"kind": "chat", "provider": "offline", "model": "offline",
            "rate": {"pricing_version": "player-smoke-offline-1",
                     "input_rmb_per_million": "1", "output_rmb_per_million": "1",
                     "cached_input_rmb_per_million": "1",
                     "multiplier": "1"}}},
        "routes": {task: "offline" for task in local.routes},
    }), encoding="utf-8")
    portal = build_portal(
        ROOT / "examples" / "catalog.json", load_settings(config), str(args.db),
        prologue_generator=offline_prologue, runtime_factory_builder=offline_runtime,
    )
    uvicorn.run(create_app(portal), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
