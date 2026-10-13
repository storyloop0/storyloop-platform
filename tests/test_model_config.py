import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

from openai.types.chat import ChatCompletion

from storyloop_platform.config import load_settings, default_settings, ModelFactory
from storyloop_platform.adapters.store import SQLiteGameStore
from storyloop_harness import TurnEngine, TurnInput
from storyloop_harness import ScenarioPackage
from storyloop_harness.generation import GenerationBudget


class ModelFactoryTests(unittest.TestCase):
    def test_model_timeouts_and_missing_credentials(self):
        settings = default_settings()
        with self.assertRaisesRegex(ValueError, 'STORY_MODEL_API_KEY'):
            ModelFactory(settings, env={}).create_model('single_turn')
        model = ModelFactory(settings, env={'STORY_MODEL_API_KEY': 'test-token'}).create_model('single_turn')
        self.assertEqual(model.client.timeout.connect, 10)
        self.assertEqual(model.client.timeout.read, 90)
        self.assertEqual(model.client.max_retries, 2)
        self.assertEqual(model.generation_budget, GenerationBudget(max_requests=3, timeout_seconds=90))


class ToolChoiceCompatibilityTests(unittest.IsolatedAsyncioTestCase):


    async def test_single_turn_sends_auto_to_qwen_compatible_endpoint(self) -> None:
        root = Path(__file__).resolve().parents[1]
        package = ScenarioPackage.load(root / "examples" / "freeform")
        config = load_settings(root / "config" / "local.json")
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteGameStore(str(Path(directory) / "game.sqlite3"))
            package.seed_game(store, "game")
            model = ModelFactory(config, env={"STORY_MODEL_API_KEY": "test-token"}).create_model("single_turn")
            decision = {"prose": "你听见码头工放下缆绳。", "participants": ["dockhand"],
                        "replies": [{"actor_id": "dockhand", "speech": "今天有船靠岸。"}]}
            choices: list[str | None] = []

            async def completion(**kwargs: object) -> ChatCompletion:
                choice = kwargs.get("tool_choice")
                choices.append(choice if isinstance(choice, str) else None)
                tool_names = [item.get("function", {}).get("name")
                              for item in kwargs.get("tools", [])]
                message = (
                    {"role": "assistant", "content": "我会和码头工打招呼。"}
                    if choice != "auto" or "generate_structured_output" not in tool_names else
                    {"role": "assistant", "content": None, "tool_calls": [{
                        "id": "decision-1", "type": "function", "function": {
                            "name": "generate_structured_output", "arguments": json.dumps(decision),
                        },
                    }]}
                )
                return ChatCompletion.model_validate({
                    "id": "chatcmpl-test", "created": 0, "model": "qwen3.8-max",
                    "object": "chat.completion", "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}, "choices": [{
                        "finish_reason": "tool_calls" if choice == "auto" and "generate_structured_output" in tool_names else "stop",
                        "index": 0, "message": message,
                    }],
                })

            model.client.chat.completions.create = AsyncMock(side_effect=completion)
            result = await TurnEngine(store, package, model).run_turn(
                TurnInput("game", "hi", "endpoint-turn", package.version))

            self.assertEqual(result.decision.target_ids, ["dockhand"])
            self.assertIn("码头工", result.narration)
            self.assertTrue(store.event_exists("game", "endpoint-turn:input:reply:dockhand:spoken"))
            self.assertIn("auto", choices)
            self.assertNotIn("required", choices)


if __name__ == "__main__":
    unittest.main()
