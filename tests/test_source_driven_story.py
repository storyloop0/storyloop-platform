"""A prepared blueprint save reads its opening and uses one story call per turn."""

import asyncio
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from storyloop_platform.generators.story_opening import StoryOpeningGenerator
from storyloop_platform.portal.http_api import create_app
from storyloop_platform.config import load_settings
from storyloop_platform.bootstrap import build_portal
from runtime_fakes import StructuredOfflineModel, offline_runtime_builder
from scenario_fixtures import synthetic_package


ROOT = Path(__file__).resolve().parents[1]


class SourceDrivenStoryTests(unittest.TestCase):
    def test_opening_retries_once_when_generated_names_collide(self):
        blueprint_data = {
            "source_document": "Test source",
            "opening_focus": "A visitor reaches the harbor and waits at its gate.",
            "setup": {
                "player_options": [{"id": "random", "label": "Random", "guidance": "Visitor",
                                    "source_ref": "Source"}],
                "tone_options": [{"id": "slow", "label": "Slow", "guidance": "Patient",
                                  "source_ref": "Source"}],
            },
            "actor_slots": [
                {"actor_id": "first", "brief": "A harbor worker with a warm greeting.",
                 "source_ref": "Source"},
                {"actor_id": "second", "brief": "Another worker who manages boats.",
                 "source_ref": "Source"},
            ],
            "facts": [{"id": "harbor", "text": "Visitors arrive by boat.",
                       "source_ref": "Source", "visibility": "public"}],
        }
        actors = [{'id': actor, 'name': actor, 'card': f'{actor}_card'} for actor in ('first', 'second')]
        book = {'package_id': 'harbor-freeform', 'version': '1.0.0', 'entries': [
            {'id': actor['card'], 'text': 'Synthetic harbor worker.', 'visibility': 'actor',
             'allowed_actors': [actor['id']], 'kind': 'card'} for actor in actors]}
        package = synthetic_package(manifest_changes={'actors': actors}, worldbook=book, blueprint=blueprint_data)

        class FakeModel:
            def __init__(self):
                self.calls = 0

            async def __call__(self, _prompt, *, structured_model):
                self.calls += 1
                names = ["阿岚", "阿岚" if self.calls == 1 else "小舟"]
                return SimpleNamespace(metadata={
                    "prose": "你走进港口，船还没有靠岸。码头上有人招手，告示牌旁有人搬运木箱。"
                             "一阵海风吹过来，远处的钟声响起，你站在入口看着两条不同的路。"
                             "工作人员正等着你决定从哪里开始，今日的船班也写在墙上。"
                             "你看见一艘船正在靠近港口，码头工人开始清理泊位，等着第一位乘客下船。",
                    "player_profile": {"姓名": "访客", "所在大学": "海洋大学"},
                    "actors": [{"actor_id": actor_id, "name": name,
                                "role_card": "这是一位在港口工作多年的角色，有自己的经历与说话方式。",
                                "public_profile": "身穿外套，正忙着自己的工作。"
                                if actor_id == "first" else "尚未登场"}
                               for actor_id, name in zip(("first", "second"), names)],
                    "options": [{"label": "问路", "input": "我向工作人员问路。"},
                                {"label": "看船", "input": "我走向码头看船。"},
                                {"label": "看告示", "input": "我查看墙上的告示。"}],
                })

        model = FakeModel()
        opening = asyncio.run(StoryOpeningGenerator(model).generate(
            "retry", package, "Harbor", {"player": "random", "tone": "slow"}))
        self.assertEqual(model.calls, 2)
        self.assertEqual([actor.name for actor in opening.actors], ["阿岚", "小舟"])
        self.assertEqual(opening.player_profile["name"], "访客")
        self.assertEqual(opening.player_profile["school"], "海洋大学")


    def test_blueprint_save_uses_prepared_opening_without_model_and_remembers_actor(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            package = directory / "package"
            shutil.copytree(ROOT / "examples/freeform", package)
            manifest_path = package / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["story_blueprint"] = "story_blueprint.json"
            manifest["actors"][0]["name"] = "阿岚"
            manifest["actors"][0]["public_profile"] = "穿着旧外套，正整理码头上的绳索。"
            manifest["authored_prologue"] = (
                "{{player_intro}}\n\n你沿着港口走来，听见绳索轻轻敲在木桩上。"
                "远处的船还没有靠岸，你可以先观察，也可以走过去打招呼。"
            )
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            worldbook_path = package / manifest["worldbook"]
            worldbook = json.loads(worldbook_path.read_text(encoding="utf-8"))
            for entry in worldbook["entries"]:
                if entry["id"] == manifest["actors"][0]["card"]:
                    entry["text"] = ("阿岚在码头工作多年，熟悉每天的船期和来往旅客。"
                                     "她说话谨慎，会先弄清对方想找什么，再告诉对方自己亲眼看到的事。"
                                     "她记得自己经历过的事情，但不会知道未被告知的秘密。"
                                     "她与附近的船长相熟，遇到陌生旅客时习惯先确认对方的目的地。")
            worldbook_path.write_text(json.dumps(worldbook, ensure_ascii=False), encoding="utf-8")
            blueprint = {
                "source_document": "Harbor source document",
                "opening_focus": "A new visitor arrives at a harbor and pauses before speaking.",
                "setup": {
                    "player_options": [{"id": "random", "label": "Random player",
                                        "guidance": "Create a visitor.", "source_ref": "Start section"},
                                       {"id": "custom", "label": "Custom player",
                                        "guidance": "Use the supplied identity.", "source_ref": "Start section"}],
                    "tone_options": [{"id": "slow", "label": "Slow story",
                                      "guidance": "Use patient pacing.", "source_ref": "Tone section"}],
                    "custom_fields": [{"id": "name", "label": "Name", "required": True,
                                       "max_length": 30}],
                },
                "actor_slots": [{"actor_id": "dockhand", "brief": "The first local character.",
                                 "source_ref": "Cast section"}],
                "player_profiles": [{"name": "访客", "appearance": "背着行囊"}],
                "opening_options": [{"label": "打招呼", "input": "我向阿岚打招呼。"},
                                    {"label": "看船", "input": "我看看远处的船。"},
                                    {"label": "问路", "input": "我问阿岚去哪找旅店。"}],
                "facts": [{"id": "harbor", "text": "The harbor is open to visitors.",
                           "source_ref": "World section", "visibility": "public"}],
                "milestones": [{"id": "arrival", "day": 1,
                                "cue": "The visitor can meet a local character.",
                                "source_ref": "Opening section", "required": True},
                               {"id": "ending", "day": 1,
                                "cue": "The visitor may leave the harbor after talking.",
                                "source_ref": "Ending section", "terminal": True}],
            }
            (package / "story_blueprint.json").write_text(json.dumps(blueprint), encoding="utf-8")
            program = {"id": manifest["id"], "ticks_per_day": manifest["ticks_per_day"],
                       "final_tick": 2,
                       "steps": [{"id": "legacy_choice", "at": 0, "kind": "choice",
                                  "prompt": "Legacy choice", "options": [
                                      {"id": "wait", "label": "Wait"}]},
                                 {"id": "ending", "at": 2, "kind": "finale",
                                  "choice_key": "legacy_choice", "threshold": 0,
                                  "success_text": "End", "other_text": "End"}]}
            # The blueprint route deliberately ignores legacy campaign scenes.
            (package / "campaign.json").write_text(json.dumps(program), encoding="utf-8")
            catalog = directory / "catalog.json"
            catalog.write_text(json.dumps({"games": [{"id": "harbor", "title": "Harbor",
                "mode": "campaign", "play_modes": ["campaign", "freeform"],
                "package": "package"}]}), encoding="utf-8")
            config_data = json.loads((ROOT / "config/local.json").read_text(encoding="utf-8"))
            config_path = directory / "config.json"
            config_path.write_text(json.dumps(config_data), encoding="utf-8")
            requests = []

            def fake_turn(request):
                requests.append(request)
                return {
                    "beats": [
                        {"kind": "narration", "text": "你走向码头工，先问候了一声。她把绳索放好。"},
                        {"kind": "dialogue", "actor_id": "dockhand", "text": "我叫阿岚，船还没进港。"},
                        {"kind": "dialogue", "actor_id": "dockhand", "text": "船期写在告示上。"}],
                    "participants": ["dockhand"], "delivery": "targets",
                    "encounters": [{"actor_id": "dockhand", "evidence": "我叫阿岚",
                                    "name_learned": True}],
                    "milestones_fulfilled": ["arrival"] if len(requests) == 1 else ["ending"],
                    "options": [{"label": "问船期", "input": "我问阿岚船什么时候到。"},
                                {"label": "看告示", "input": "我去看告示板。"},
                                {"label": "继续闲聊", "input": "我和阿岚聊聊港口生活。"}],
                }

            with patch.dict(os.environ, {"STORY_MODEL_API_KEY": "offline-test"}), \
                    patch("storyloop_platform.generators.story_opening.StoryOpeningGenerator.generate",
                          side_effect=AssertionError("opening must not call a model")):
                portal = build_portal(catalog, load_settings(config_path),
                                      str(directory / "game.sqlite3"),
                                      runtime_factory_builder=offline_runtime_builder(model=StructuredOfflineModel(fake_turn)))
                token = portal.register("source-player", "password-123")["token"]
                first = asyncio.run(portal.create_save(token, "harbor", "campaign",
                                                       {"player": "random", "tone": "slow"}))
                self.assertIn("你沿着港口走来", first["opening"])
                self.assertIn("你是访客", first["opening"])
                self.assertIsNone(first["interaction"])
                self.assertEqual(len(first["action_options"]), 3)
                custom = asyncio.run(portal.create_save(token, "harbor", "campaign",
                    {"player": "custom", "tone": "slow", "name": "阿禾"}))
                self.assertIn("你是阿禾", custom["opening"])
                self.assertEqual(portal.player_card(token, custom["game_id"])["name"], "阿禾")
                save_id = first["game_id"]
                self.assertEqual(portal.cast(token, save_id), [])
                stranger = portal.register("other-player", "password-123")["token"]
                with TestClient(create_app(portal), base_url="http://127.0.0.1") as client:
                    own_card = client.get(f"/v1/saves/{save_id}/player-card", headers={
                        "Authorization": f"Bearer {token}",
                    })
                    self.assertEqual(own_card.status_code, 200)
                    self.assertEqual(own_card.json(), {"name": "访客", "fields": [
                        {"key": "appearance", "value": "背着行囊"},
                    ]})
                    self.assertEqual(client.get(f"/v1/saves/{save_id}/player-card", headers={
                        "Authorization": f"Bearer {stranger}",
                    }).status_code, 404)
                self.assertEqual(asyncio.run(portal.resume_save(token, save_id))["opening"], "")
                turn = asyncio.run(portal.turn(token, save_id, "我向阿岚打招呼。", "turn-1"))
                self.assertIn("我叫阿岚，船还没进港", turn["body"])
                self.assertEqual(turn["body"].count("我叫阿岚，船还没进港。"), 1)
                self.assertEqual([actor["name"] for actor in portal.cast(token, save_id)], ["阿岚"])
                self.assertEqual(requests[0]["source_story_tone"], "Use patient pacing.")
                self.assertEqual(requests[0]["player_identity_knowledge"]["named_actor_ids"], [])
                self.assertIn("远处的船还没有靠岸", str(requests[0]["recent_visible_beats"]))
                self.assertEqual(requests[0]["npc_contexts"][0]["name"], "阿岚")
                self.assertTrue(any("船还没进港" in entry.content for entry in
                                    portal.gameplay.store.agent_context_entries(save_id, "dockhand")))
                self.assertTrue(portal.gameplay.store.load(save_id).data["story_progress"]["completed"]["arrival"])
                second = asyncio.run(portal.turn(token, save_id, "船来了么？", "turn-2"))
                self.assertEqual(second["day"], 1)
                self.assertTrue(second["complete"])
                self.assertEqual(second["action_options"], [])
                self.assertIn("船还没进港", str(requests[1]["npc_contexts"][0]["own_history"]))
                self.assertEqual(requests[1]["player_identity_knowledge"]["named_actor_ids"], ["dockhand"])
                self.assertIn("你走向码头工", str(requests[1]["recent_visible_beats"]))
                self.assertEqual([item["id"] for item in requests[1]["source_story_milestones"]],
                                 ["ending"])
                with self.assertRaisesRegex(ValueError, "story is complete"):
                    asyncio.run(portal.turn(token, save_id, "再来一轮", "turn-3"))
                portal.close()
