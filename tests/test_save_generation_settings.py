"""Settings belong to one save and can expand a previously compressed context."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import text

from storyloop_platform.portal.http_api import create_app
from storyloop_platform.config import load_settings
from storyloop_platform.bootstrap import build_portal
from runtime_fakes import OfflineRuntimeFactory
from storyloop_harness import ScenarioPackage, TurnEngine


ROOT = Path(__file__).resolve().parents[1]


class SaveGenerationSettingsTests(unittest.TestCase):
    def test_owner_only_settings_persist_and_larger_window_rebuilds_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            portal = build_portal(ROOT / "examples/catalog.json",
                                  load_settings(ROOT / "config/local.json"), str(Path(temp) / "game.sqlite3"),
                                  runtime_factory_builder=OfflineRuntimeFactory)
            owner = portal.register("settings-owner", "password-123")
            stranger = portal.register("settings-stranger", "password-123")
            portal.accounts.create_save(owner["player_id"], "sample", "game-1", "sample", "1", "hash")
            with TestClient(create_app(portal), base_url="http://127.0.0.1") as client:
                own = {"Authorization": f"Bearer {owner['token']}"}
                other = {"Authorization": f"Bearer {stranger['token']}"}
                defaults = client.get("/v1/saves/game-1/settings", headers=own)
                self.assertEqual(defaults.status_code, 200)
                self.assertEqual(defaults.json()["temperature"], 1.0)
                self.assertEqual(client.get("/v1/saves/game-1/settings", headers=other).status_code, 404)
                self.assertEqual(client.put("/v1/saves/game-1/settings", headers=other,
                    json={"temperature": 0.5, "context_window_tokens": 32768}).status_code, 404)

                first = client.put("/v1/saves/game-1/settings", headers=own,
                    json={"temperature": 0.5, "context_window_tokens": 32768})
                self.assertEqual(first.status_code, 200)
                self.assertEqual(first.json()["context_window_tokens"], 32768)
                self.assertEqual(client.put("/v1/saves/game-1/settings", headers=own,
                    json={"temperature": 2.0, "context_window_tokens": 32768}).status_code, 400)

                with portal.accounts.engine.begin() as db:
                    db.execute(text("""INSERT INTO agent_contexts
                        (game_id,actor_id,through_version,summary)
                        VALUES ('game-1','player',3,'compressed history')"""))
                bigger = client.put("/v1/saves/game-1/settings", headers=own,
                    json={"temperature": 0.8, "context_window_tokens": 65536})
                self.assertEqual(bigger.status_code, 200)
                with portal.accounts.engine.connect() as db:
                    count = db.execute(text("SELECT COUNT(*) FROM agent_contexts WHERE game_id='game-1'")).scalar()
                self.assertEqual(count, 0)
                self.assertEqual(client.get("/v1/saves/game-1/settings", headers=own).json()["temperature"], 0.8)

                item = portal.gameplay.game_access.listing_for(owner["player_id"], "npc-chat")
                package = ScenarioPackage.load(item.package_path)
                with patch.object(portal.gameplay.factory.models, "create_model", return_value=object()) as factory, \
                        patch("storyloop_platform.gameplay.factory.TurnEngine", wraps=TurnEngine) as engine_factory:
                    first_session = portal.gameplay.factory.engine(item, package, "game-1")
                    self.assertEqual(factory.call_args.kwargs["temperature"], 0.8)
                    self.assertEqual(engine_factory.call_args.kwargs["context_budget"].context_window_tokens, 65536)
                    client.put("/v1/saves/game-1/settings", headers=own,
                        json={"temperature": 1.2, "context_window_tokens": 32768})
                    second_session = portal.gameplay.factory.engine(item, package, "game-1")
                    self.assertIsNot(first_session, second_session)
                    self.assertEqual(factory.call_args.kwargs["temperature"], 1.2)
                    self.assertEqual(engine_factory.call_args.kwargs["context_budget"].context_window_tokens, 32768)
