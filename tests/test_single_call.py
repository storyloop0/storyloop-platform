"""Focused contract checks for the default one-call story engine."""

from runtime_fakes import StructuredOfflineModel

import tempfile
import unittest
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

from storyloop_harness import TurnEngine
from storyloop_platform.adapters.store import SQLiteGameStore
from storyloop_platform.generators.scene_messages import MessageScene
from storyloop_platform.runtime.campaign import CampaignProgram, CampaignSession
from storyloop_harness.advanced import StoryClock
from storyloop_harness import ScenarioPackage


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "freeform"


class SingleCallTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = SQLiteGameStore(str(Path(temp.name) / "game.sqlite3"))
        self.package = replace(ScenarioPackage.load(EXAMPLE), presentation_mode="novel")
        self.package.seed_game(self.store, "game")


    async def test_campaign_skips_second_presentation_call_for_free_action(self):
        program = CampaignProgram.from_dict({
            "id": self.package.package_id, "ticks_per_day": 4, "final_tick": 4,
            "steps": [
                {"id": "arrival", "at": 0, "kind": "scene", "text": "来到港口。"},
                {"id": "choice", "at": 2, "kind": "choice", "prompt": "找谁？",
                 "options": [{"id": "dockhand", "label": "码头工"}]},
                {"id": "end", "at": 4, "kind": "finale", "choice_key": "choice",
                 "threshold": 0, "success_text": "结束", "other_text": "结束"},
            ],
        })
        before = self.store.load("game")
        state = deepcopy(before.data)
        state["campaign"] = program.initial_state(["dockhand"])
        self.store.create_game(replace(before, game_id="campaign", data=state))
        model = StructuredOfflineModel(lambda _: {
            'beats': [{'kind': 'narration', 'text': '你听见码头工放下缆绳。'},
                      {'kind': 'dialogue', 'actor_id': 'dockhand', 'text': '今天有船靠岸。'}],
            'participants': ['dockhand'],
        })
        clock = StoryClock(8, overnight_requires_rest=True)
        engine = TurnEngine(self.store, self.package, model, clock=clock, program=program)

        class ForbiddenPresenter:
            async def present(self, context):
                raise AssertionError("ordinary turn must not call a second model")

        campaign = CampaignSession(self.store, program, engine, turns_per_story_tick=1,
                                   novel_presenter=ForbiddenPresenter(),
                                   scene_turn_includes_presentation=True)
        await campaign.start("campaign", present_opening=False)
        outcome = await campaign.submit("campaign", "你好", "turn-1")

        self.assertEqual(len(model.requests), 1)
        self.assertIn("码头工", outcome.text)
        self.assertFalse(any(item.kind == "time" for item in outcome.segments))

    async def test_message_gate_uses_one_batch_writer_and_saves_recipient_memory(self):
        program = CampaignProgram.from_dict({
            "id": self.package.package_id, "ticks_per_day": 4, "final_tick": 4,
            "steps": [
                {"id": "intro", "at": 0, "kind": "scene", "text": "夜里收到愿望卡。"},
                {"id": "partner", "at": 0, "kind": "choice", "prompt": "认识谁？",
                 "options": [{"id": "dockhand", "label": "码头工", "meet": "dockhand"}]},
                {"id": "message", "at": 0, "kind": "message", "prompt": "给谁留言？",
                 "options": [{"id": "dockhand", "label": "码头工", "recipient": "dockhand"}],
                 "incoming": [{"actor": "dockhand", "label": "码头工", "text": "剧本备用短信",
                               "min_affinity": 0}]},
                {"id": "end", "at": 4, "kind": "finale", "choice_key": "partner",
                 "threshold": 0, "success_text": "结束", "other_text": "结束"},
            ],
        })
        before = self.store.load("game")
        state = deepcopy(before.data)
        state["campaign"] = program.initial_state(["dockhand"])
        self.store.create_game(replace(before, game_id="message-game", data=state))

        class BatchWriter:
            def __init__(self):
                self.calls = []

            async def write(self, game_id, senders, recipient, player_note, fallback):
                self.calls.append((senders, recipient, player_note))
                return MessageScene("夜色里，留言送了出去。", {"dockhand": "我也想再聊聊。"})

        writer = BatchWriter()
        campaign = CampaignSession(self.store, program,
                                   turns_per_story_tick=1,
                                   message_batch_writer=writer,
                                   scene_turn_includes_presentation=True)
        await campaign.start("message-game", present_opening=False)
        await campaign.submit("message-game", "/choose dockhand", "partner-turn")
        outcome = await campaign.submit("message-game", "/choose dockhand 你好", "message-turn")

        self.assertEqual(writer.calls, [(('dockhand',), 'dockhand', '你好')])
        self.assertIn("我也想再聊聊", outcome.text)
        self.assertTrue(any(item.content == "你好" for item in
                            self.store.observations_for("message-game", "dockhand")))


if __name__ == "__main__":
    unittest.main()
