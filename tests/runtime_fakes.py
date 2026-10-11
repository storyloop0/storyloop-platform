"""Offline public runtime builder seams for Platform integration tests."""

import json
from types import SimpleNamespace

from storyloop_harness.testing import OfflineModel
from storyloop_harness import TurnOutcome
from storyloop_harness.advanced import MainDecision, RunResult
from storyloop_harness.usage import record_model_usage
from storyloop_platform.gameplay.factory import RuntimeFactory


class OfflineModels:
    def __init__(self, model=None):
        self.model = model or OfflineModel()
        self.calls = []

    def require_credentials(self):
        pass

    def create_model(self, task, *, temperature=None):
        self.calls.append((task, temperature))
        return self.model


class OfflineRuntimeFactory(RuntimeFactory):
    def __init__(self, *, model=None, executor_builder=None, presenter=None, **dependencies):
        self.offline_models = OfflineModels(model)
        dependencies["models"] = self.offline_models
        super().__init__(**dependencies)
        self.executor_builder = executor_builder
        self.presenter = presenter

    def engine(self, item, package, game_id, **kwargs):
        if self.executor_builder is not None:
            return self.executor_builder(item, package, game_id, **kwargs)
        return super().engine(item, package, game_id, **kwargs)

    def novel_presenter(self, package, game_id):
        if self.presenter is not None:
            return self.presenter
        return super().novel_presenter(package, game_id)


class OfflineExecutor:
    """Base implementing the public executor shape for targeted turn fakes."""
    def __init__(self, store, package):
        self.store = store
        self.package = package

    def proposed_options(self, game_id, turn_id, authored=()):
        return authored

    async def run_ready_work(self, game_id, progress=None):
        return RunResult((), (), self.store.load(game_id))


def offline_runtime_builder(*, model=None, executor_builder=None, presenter=None):
    def build(**dependencies):
        return OfflineRuntimeFactory(model=model, executor_builder=executor_builder,
                                     presenter=presenter, **dependencies)
    return build


def turn_outcome(snapshot, narration):
    return TurnOutcome(MainDecision(intent="speech"), narration, (), (), snapshot)


class StructuredOfflineModel:
    def __init__(self, response_for_request, *, model="deepseek-v4.1-flash", input_tokens=0,
                 output_tokens=0):
        self.response_for_request = response_for_request
        self.model = model
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.requests = []

    async def __call__(self, messages, *, structured_model, **kwargs):
        content = messages[-1]["content"]
        if isinstance(content, list):
            content = "".join(part["text"] for part in content if part.get("type") == "text")
        request = json.loads(content)
        self.requests.append(request)
        result = self.response_for_request(request)
        usage = SimpleNamespace(input_tokens=self.input_tokens, output_tokens=self.output_tokens)
        record_model_usage(self.model, "single_turn", usage)
        return SimpleNamespace(metadata=result, usage=usage)
