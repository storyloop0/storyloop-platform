"""Captured, credential-free construction terms for ordinary scene recovery."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from storyloop_harness import ScenarioPackage, TurnEngine
from storyloop_harness.context import ContextBudget

from storyloop_platform.portal.catalog import GameListing
from storyloop_platform.portal.save_generation_settings import SaveGenerationSettings


@dataclass(frozen=True)
class SceneRecoveryPlan:
    generation_identity: str
    generation_settings: SaveGenerationSettings
    context_budget: ContextBudget
    model_config_json: str
    base_url: str
    max_responders: int
    max_steps: int
    turns_per_story_tick: int
    execution_max_requests: int
    execution_timeout_seconds: float


@runtime_checkable
class SceneRecoveryRuntime(Protocol):
    def scene_recovery_plan(self, item: GameListing, package: ScenarioPackage,
                            game_id: str) -> SceneRecoveryPlan | None: ...

    def scene_recovery_engine(self, item: GameListing, package: ScenarioPackage,
                              game_id: str, *, plan: SceneRecoveryPlan,
                              recovery_only: bool) -> TurnEngine: ...


class ForbiddenRecoveryModel:
    """Reject accidental generation from a recovery-only TurnEngine."""

    async def __call__(self, *args: object, **kwargs: object) -> object:
        raise RuntimeError("scene recovery cannot invoke a model")
