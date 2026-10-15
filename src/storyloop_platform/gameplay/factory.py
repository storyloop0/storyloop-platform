"""Public gameplay runtime contracts and model/campaign assembly."""

from __future__ import annotations

import hashlib
import json
from importlib.metadata import version
from pathlib import Path
from typing import Protocol

from storyloop_harness import GameStore, ScenarioPackage, TurnEngine, TurnInput, TurnOutcome
from storyloop_harness.advanced import TurnProgress, StoryClock, RunResult, GeneratedSceneRecovery
from storyloop_harness.generation import ActionOption
from storyloop_harness.context import ContextBudget, ContextProjection
from storyloop_harness.telemetry import Telemetry

from storyloop_platform.config import PlatformSettings, ModelFactory
from storyloop_platform.gameplay.access import require_supported_save
from storyloop_platform.gameplay.scene_recovery import ForbiddenRecoveryModel, SceneRecoveryPlan
from storyloop_platform.config.schema import ChatModelSettings
from storyloop_platform.generators.novel_narrator import NovelTurnNarrator
from storyloop_platform.generators.scene_messages import SingleCallMessageWriter
from storyloop_platform.generators.scene_narrator import CampaignSceneNarrator
from storyloop_platform.portal.catalog import GameListing
from storyloop_platform.portal.save_generation_settings import SaveGenerationSettings, SQLSaveGenerationSettings
from storyloop_platform.portal.sql_repository import SQLPlayerRepository
from storyloop_platform.runtime.campaign import CampaignProgram, CampaignSession

PLATFORM_SCENE_CONTRACT = "platform-ordinary-scene-v1"
AGENTSCOPE_COMPATIBLE_CONTRACT = "agentscope-compatible-v1"


class TurnExecutor(Protocol):
    package: ScenarioPackage
    def project_context(self, turn: TurnInput) -> ContextProjection: ...

    async def run_turn(self, turn: TurnInput, *, progress: TurnProgress | None = None,
                       max_tick: int | None = None) -> TurnOutcome: ...
    async def run_ready_work(self, game_id: str, progress: TurnProgress | None = None) -> RunResult: ...
    def proposed_options(self, game_id: str, turn_id: str,
                         authored: tuple[ActionOption, ...] = ()) -> tuple[ActionOption, ...]: ...


class GameplayRuntime(Protocol):
    def default_generation_settings(self) -> SaveGenerationSettings: ...
    def generation_settings(self, game_id: str) -> SaveGenerationSettings: ...
    def context_budget(self, settings: SaveGenerationSettings) -> ContextBudget: ...
    def invalidate(self, game_id: str) -> None: ...
    def require_credentials(self) -> None: ...
    def engine(self, item: GameListing, package: ScenarioPackage, game_id: str, *,
               overnight_requires_rest: bool = False,
               program: CampaignProgram | None = None) -> TurnExecutor: ...
    def campaign(self, item: GameListing, package: ScenarioPackage,
                 game_id: str) -> tuple[CampaignProgram, CampaignSession]: ...
    def novel_presenter(self, package: ScenarioPackage, game_id: str) -> NovelTurnNarrator: ...


class RuntimeFactoryBuilder(Protocol):
    def __call__(self, *, settings: PlatformSettings, store: GameStore,
                 accounts: SQLPlayerRepository, generation_settings: SQLSaveGenerationSettings,
                 models: ModelFactory, telemetry: Telemetry) -> GameplayRuntime: ...


class RuntimeFactory:
    def __init__(self, *, settings: PlatformSettings, store: GameStore,
                 accounts: SQLPlayerRepository, generation_settings: SQLSaveGenerationSettings,
                 models: ModelFactory, telemetry: Telemetry) -> None:
        self.settings = settings
        self.store = store
        self.accounts = accounts
        self.generation_settings_store = generation_settings
        self.models = models
        self.telemetry = telemetry

        self._turn_engines: dict[tuple, TurnExecutor] = {}
        self._campaign_sessions: dict[tuple, tuple[CampaignProgram, CampaignSession]] = {}

    def default_generation_settings(self) -> SaveGenerationSettings:
        window = min(self.settings.context_window_for("single_turn"),
                     self.settings.runtime.context_window_tokens)
        temperature = self.settings.model_for("single_turn").generation.temperature
        temperature = 1.0 if temperature is None else temperature
        return SaveGenerationSettings(temperature, window)

    def generation_settings(self, game_id: str) -> SaveGenerationSettings:
        defaults = self.default_generation_settings()
        saved = self.generation_settings_store.get(game_id, defaults)
        return SaveGenerationSettings(saved.temperature,
                                      min(saved.context_window_tokens, defaults.context_window_tokens))

    def context_budget(self, settings: SaveGenerationSettings) -> ContextBudget:
        window = min(settings.context_window_tokens,
                     self.default_generation_settings().context_window_tokens)
        output = self.settings.model_for("single_turn").generation.max_tokens
        output = 4096 if output is None else output
        try:
            return ContextBudget(context_window_tokens=window, output_tokens=output)
        except ValueError:
            raise ValueError(
                f"single_turn context window {window} must exceed generation.max_tokens "
                f"{output} plus safety margin 256; increase the save/model/runtime window "
                "or reduce single_turn generation.max_tokens"
            ) from None

    def scene_recovery_plan(self, item: GameListing, package: ScenarioPackage,
                            game_id: str) -> SceneRecoveryPlan | None:
        # Subclasses must explicitly implement both capability methods. In particular,
        # OfflineRuntimeFactory overrides engine and must retain its legacy path.
        if type(self) is not RuntimeFactory or not (
            type(self.models) is ModelFactory or
            type(self.models).__dict__.get("native_scene_recovery_model_factory") is True
        ):
            return None
        if item.mode != "freeform" or package.presentation_mode != "interactive" or package.story_blueprint is not None:
            return None
        settings = self.generation_settings(game_id)
        budget = self.context_budget(settings)
        definition = self.settings.model_for("single_turn")
        if not isinstance(definition, ChatModelSettings):
            return None
        captured = definition.model_dump(mode="json")
        captured["generation"]["temperature"] = settings.temperature
        captured_model = ChatModelSettings.model_validate(captured)
        base_url = self.models.base_url(captured_model.provider)
        runtime = self.settings.runtime
        # The provider selector stays in the in-memory profile for late credential
        # lookup. Its environment variable name and rate have no generation effect.
        identity_model = captured_model.model_dump(mode="json")
        identity_model.pop("provider", None)
        identity_model.pop("rate", None)
        terms = {
            "contracts": [PLATFORM_SCENE_CONTRACT, AGENTSCOPE_COMPATIBLE_CONTRACT],
            "harness_version": version("storyloop-harness"),
            "endpoint_sha256": hashlib.sha256(base_url.encode("utf-8")).hexdigest(),
            "model": identity_model,
            "context_budget": {
                "context_window_tokens": budget.context_window_tokens,
                "output_tokens": budget.output_tokens,
            },
            "max_responders": runtime.max_npc_replies,
            "max_steps": runtime.max_steps,
            "ticks_per_day": package.ticks_per_day,
            "turns_per_story_tick": item.turns_per_story_tick,
            "execution_max_requests": runtime.turn_max_requests,
            "execution_timeout_seconds": float(runtime.turn_timeout_seconds),
        }
        canonical = json.dumps(terms, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return SceneRecoveryPlan(
            generation_identity=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            generation_settings=settings, context_budget=budget,
            model_config_json=captured_model.model_dump_json(), base_url=base_url,
            max_responders=runtime.max_npc_replies, max_steps=runtime.max_steps,
            turns_per_story_tick=item.turns_per_story_tick,
            execution_max_requests=runtime.turn_max_requests,
            execution_timeout_seconds=float(runtime.turn_timeout_seconds),
        )

    def scene_recovery_engine(self, item: GameListing, package: ScenarioPackage,
                              game_id: str, *, plan: SceneRecoveryPlan,
                              recovery_only: bool) -> TurnEngine:
        if type(self) is not RuntimeFactory or not (
            type(self.models) is ModelFactory or
            type(self.models).__dict__.get("native_scene_recovery_model_factory") is True
        ):
            raise ValueError("scene recovery runtime is unsupported")
        if item.mode != "freeform" or package.presentation_mode != "interactive" or package.story_blueprint is not None:
            raise ValueError("scene recovery runtime is unsupported")
        require_supported_save(self.store, game_id)
        key = (item.package_path, game_id, plan.generation_identity,
               recovery_only, package.version)
        if key in self._turn_engines:
            return self._turn_engines[key]
        model = (ForbiddenRecoveryModel() if recovery_only else
                 self.models.create_resolved_model(
                     task="single_turn", model_config_json=plan.model_config_json,
                     base_url=plan.base_url))
        clock = (StoryClock(package.ticks_per_day * plan.turns_per_story_tick)
                 if package.ticks_per_day is not None else None)
        engine = TurnEngine(
            self.store, package, model,
            max_responders=plan.max_responders, context_budget=plan.context_budget,
            clock=clock, max_steps=plan.max_steps, telemetry=self.telemetry,
            generated_scene_recovery=GeneratedSceneRecovery(
                self.store, plan.generation_identity, protect_uncertain_generation=True),
        )
        self._turn_engines[key] = engine
        if len(self._turn_engines) > 128:
            self._turn_engines.pop(next(iter(self._turn_engines)))
        return engine

    def engine(self, item: GameListing, package: ScenarioPackage, game_id: str,
                     *, overnight_requires_rest: bool = False,
                     program: CampaignProgram | None = None) -> TurnEngine:
        require_supported_save(self.store, game_id)
        settings = self.generation_settings(game_id)
        budget = self.context_budget(settings)
        key = (item.package_path, game_id, settings, budget, overnight_requires_rest,
               id(program), item.turns_per_story_tick)
        if key in self._turn_engines:
            return self._turn_engines[key]
        self.require_credentials()
        clock = (StoryClock(package.ticks_per_day * item.turns_per_story_tick,
                            overnight_requires_rest=overnight_requires_rest)
                 if package.ticks_per_day is not None else None)
        session = TurnEngine(
            self.store, package,
            self.models.create_model("single_turn",
                                     temperature=settings.temperature),
            max_responders=self.settings.runtime.max_npc_replies,
            context_budget=budget,
            program=program, clock=clock, max_steps=self.settings.runtime.max_steps,
            telemetry=self.telemetry,
        )
        self._turn_engines[key] = session
        if len(self._turn_engines) > 128:
            self._turn_engines.pop(next(iter(self._turn_engines)))
        return session

    def require_credentials(self) -> None:
        self.models.require_credentials()

    def campaign(self, item: GameListing, package: ScenarioPackage,
                  game_id: str) -> tuple[CampaignProgram, CampaignSession]:
        settings = self.generation_settings(game_id)
        key = (item.package_path, game_id, settings, self.context_budget(settings),
               item.turns_per_story_tick)
        if key in self._campaign_sessions:
            return self._campaign_sessions[key]
        program = CampaignProgram.load(item.package_path / "campaign.json")
        turn_engine = self.engine(item, package, game_id, overnight_requires_rest=True,
                                        program=program)
        result = (program, CampaignSession(
            self.store, program, turn_engine, telemetry=self.telemetry,
            turns_per_story_tick=item.turns_per_story_tick,
            scene_presenter=CampaignSceneNarrator(
                package, program, self.models.create_model("narration",
                                                           temperature=settings.temperature),
                self.telemetry,
            ) if package.presentation_mode == "interactive" else None,
            novel_presenter=self.novel_presenter(package, game_id) if package.presentation_mode == "novel" else None,
            message_batch_writer=SingleCallMessageWriter(
                self.store, package,
                self.models.create_model(
                    "single_turn",
                    temperature=settings.temperature,
                ), self.telemetry,
            ),
            scene_turn_includes_presentation=True,
        ))
        self._campaign_sessions[key] = result
        if len(self._campaign_sessions) > 128:
            self._campaign_sessions.pop(next(iter(self._campaign_sessions)))
        return result

    def novel_presenter(self, package: ScenarioPackage, game_id: str) -> NovelTurnNarrator:
        def recent_prose(save_id: str) -> list[str]:
            passages: list[str] = []
            for turn in self.accounts.list_turns(save_id)[-3:]:
                response = turn.get("response", {})
                if not isinstance(response, dict):
                    continue
                for segment in response.get("segments", []):
                    if isinstance(segment, dict) and segment.get("kind") == "narration":
                        body = segment.get("text")
                        if isinstance(body, str) and body.strip():
                            passages.append(body.strip())
            return passages[-3:]

        return NovelTurnNarrator(
            package,
            self.models.create_model("narration",
                                     temperature=self.generation_settings(game_id).temperature),
            self.telemetry,
            recent_prose,
        )

    def invalidate(self, game_id: str) -> None:
        for cache in (self._turn_engines, self._campaign_sessions):
            for key in tuple(cache):
                if key[1] == game_id:
                    del cache[key]
