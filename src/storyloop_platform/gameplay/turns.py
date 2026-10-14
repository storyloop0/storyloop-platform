"""Turn execution, recovery, and immutable usage settlement."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import nullcontext
import re
from uuid import uuid4

from storyloop_harness import GameStore, ScenarioPackage, TurnInput
from storyloop_harness.advanced import segment_for_observation, TurnProgress, emit, StoryClock, player_preferences_scope
from storyloop_harness.generation import ActionOption, ExecutionBudget, execution_budget_scope
from storyloop_harness.telemetry import Telemetry, session_id_for_game

from storyloop_platform.gameplay.access import GameAccess, require_supported_save
from storyloop_platform.gameplay.attempt_audit_scope import attempt_audit_scope
from storyloop_platform.gameplay.factory import GameplayRuntime
from storyloop_platform.gameplay.locks import PlayerTurnLocks
from storyloop_platform.gameplay.presentation import package_for_mode, turn_view
from storyloop_platform.memory.service import PlayerMemoryService
from storyloop_platform.portal.billing import collect_usage, BillingPolicy
from storyloop_platform.portal.catalog import GameListing
from storyloop_platform.portal.moderation import ScenarioModerationService
from storyloop_platform.portal.model_execution_audits import SQLModelExecutionAudits
from storyloop_platform.portal.sql_billing import SQLBillingRepository
from storyloop_platform.portal.sql_repository import SQLPlayerRepository
from storyloop_platform.portal.turn_errors import TurnInputError, TurnRecoveryRequired
from storyloop_platform.portal.turn_settlements import SQLTurnSettlements, TurnSettlement
from storyloop_platform.runtime.guidance import GuidanceAdvisor, GuidanceResult


class TurnExecutionService:
    """Ordering-sensitive execution and settlement; SQL collaborators share one Engine."""

    def __init__(self, *, store: GameStore, accounts: SQLPlayerRepository,
                 game_access: GameAccess, moderation: ScenarioModerationService,
                 settlements: SQLTurnSettlements, billing: SQLBillingRepository | None,
                 billing_for_policy: Callable[[BillingPolicy], SQLBillingRepository],
                 memory_service: PlayerMemoryService, factory: GameplayRuntime,
                 player_locks: PlayerTurnLocks, telemetry: Telemetry,
                 execution_budget: ExecutionBudget,
                 audits: SQLModelExecutionAudits | None = None) -> None:
        self.store = store
        self.accounts = accounts
        self.game_access = game_access
        self.moderation = moderation
        self.settlements = settlements
        self.billing = billing
        self.billing_for_policy = billing_for_policy
        self.memory_service = memory_service
        self.factory = factory
        self.player_locks = player_locks
        self.telemetry = telemetry
        self.execution_budget = execution_budget
        self.audits = audits if audits is not None else SQLModelExecutionAudits(accounts.engine)

    async def turn(self, player_id: str, game_id: str, text: str,
                   request_id: str | None = None, progress: TurnProgress | None = None) -> dict:
        async with self.player_locks.for_player(player_id):
            return await self._turn_for_player(player_id, game_id, text, request_id, progress)

    async def _turn_for_player(self, player_id: str, game_id: str, text: str,
                               request_id: str | None, progress: TurnProgress | None) -> dict:
        record = self.accounts.get_save(player_id, game_id)
        require_supported_save(self.store, game_id)
        item = self.game_access.verified_listing(record)
        mode = record.play_mode or item.mode
        preview = self.moderation.preview_for(game_id, player_id) is not None
        if not isinstance(text, str) or not text.strip():
            raise TurnInputError("turn text is required")
        if len(text) > 10_000:
            raise TurnInputError("turn text is too long")
        request_id = request_id or uuid4().hex
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", request_id):
            raise ValueError("request_id must contain 1–64 URL-safe characters")
        cached = self.accounts.get_turn_response(game_id, request_id, text)
        if cached is not None:
            return cached
        memory_epoch = self.memory_service.collection_epoch(player_id)
        prepared = self.settlements.get(game_id, request_id, text)
        if prepared is not None:
            return self._settle_prepared_turn(player_id, game_id, request_id, text, item,
                                              prepared, memory_epoch)
        turn_id = f"portal-{request_id}"
        progress_state = self.store.load(game_id).data.get("story_progress", {})
        committed_same_turn = any(self.store.event_exists(game_id, f"{turn_id}:{kind}")
                                  for kind in ("input", "query", "action", "continue",
                                               "choice", "advance", "completed"))
        if committed_same_turn and not preview:
            # Until model-call journaling exists, earlier interrupted turns cannot
            # be reconstructed with a fresh (empty) usage collector and billed as zero.
            raise TurnRecoveryRequired("行动已提交，但计量恢复记录不完整，请联系管理员恢复此回合。")
        if (isinstance(progress_state, dict) and progress_state.get("complete") is True
                and not committed_same_turn):
            raise TurnInputError("this story is complete")
        if preview and self.moderation.preview_turn_count(game_id) >= 12:
            raise ValueError("review preview has reached its 12-turn limit")
        if self.billing is not None and not preview:
            self.billing.require_credit(player_id)
        package = package_for_mode(ScenarioPackage.load(item.package_path), mode)
        context = collect_usage() if self.billing is not None and not preview else nullcontext(None)
        preferences = () if preview else await self.memory_service.preferences_for(player_id)
        engine_usage = ()
        with attempt_audit_scope(self.audits, player_id=player_id, game_id=game_id,
                                 request_id=request_id, player_text=text,
                                 policy=self.billing.policy if self.billing is not None and not preview else None):
            async with execution_budget_scope(self.execution_budget):
                with context as meter, player_preferences_scope(preferences):
                    source_driven = package.story_blueprint is not None
                    recovered = (await self._recover_freeform(item, package, game_id, text, turn_id,
                                                              mode)
                                 if mode == "freeform" or source_driven else None)
                    if recovered is not None:
                        view = recovered
                    elif mode == "campaign" and not source_driven:
                        program, session = self.factory.campaign(item, package, game_id)
                        single = session.turn_engine
                        outcome = await session.submit(game_id, text, turn_id, progress=progress)
                        engine_usage = outcome.model_usage
                        await emit(progress, "preview", body=outcome.text,
                                   segments=[part.to_dict() for part in outcome.segments])
                        snapshot = self.store.load(game_id)
                        await emit(progress, "stage", stage="guidance")
                        guidance = await GuidanceAdvisor(self.store, package, program,
                                                         telemetry=self.telemetry,
                                                         turns_per_story_tick=item.turns_per_story_tick).advise(
                            game_id, snapshot, gate_id=outcome.gate_id,
                            complete=outcome.complete, visible_text=outcome.text)
                        leads = program.current_action_context(snapshot).get("leads", [])
                        authored = tuple(ActionOption.model_validate(item) for item in leads)
                        action_options = (single.proposed_options(game_id, turn_id, authored)
                                          if outcome.gate_id is None and not outcome.complete
                                          else ())
                        view = turn_view(game_id, item.game_id, mode, outcome.text,
                                          snapshot, guidance, complete=outcome.complete,
                                          turn_id=turn_id, segments=outcome.segments,
                                          program=program, gate_id=outcome.gate_id,
                                          time_of_day=outcome.time_of_day,
                                          presentation_mode=package.presentation_mode,
                                          action_options=action_options,
                                          status_fields=package.status_fields, time_unit=package.time_unit)
                    else:
                        turn_engine = (self.factory.engine(item, package, game_id,
                                            overnight_requires_rest=True)
                                 if source_driven else self.factory.engine(item, package, game_id))
                        single = turn_engine
                        outcome = await turn_engine.run_turn(
                            TurnInput(game_id, text, turn_id, package.version), progress=progress,
                        )
                        engine_usage = getattr(outcome, "model_usage", ())
                        body, segments = outcome.narration, outcome.segments
                        await emit(progress, "preview", body=body,
                                   segments=[part.to_dict() for part in segments])
                        await emit(progress, "stage", stage="guidance")
                        guidance = (GuidanceResult((), "source_story") if source_driven else
                                    await GuidanceAdvisor(self.store, package, telemetry=self.telemetry).advise(
                                        game_id, self.store.load(game_id), visible_text=body))
                        action_options = single.proposed_options(game_id, turn_id)
                        clock = (StoryClock(package.ticks_per_day * item.turns_per_story_tick)
                                 if package.ticks_per_day is not None else None)
                        view = turn_view(game_id, item.game_id, mode, body,
                                          self.store.load(game_id), guidance, turn_id=turn_id,
                                          segments=segments,
                                          presentation_mode=package.presentation_mode,
                                          action_options=action_options, clock=clock,
                                          status_fields=package.status_fields, time_unit=package.time_unit)
        if self.billing is not None and not preview:
            prepared = self.settlements.prepare(game_id, request_id, text, view,
                                                     list(engine_usage) + meter.records, self.billing.policy)
            return self._settle_prepared_turn(player_id, game_id, request_id, text, item,
                                              prepared, memory_epoch)
        self.accounts.store_turn_response(game_id, request_id, text, view)
        if not preview:
            self.memory_service.queue_input(player_id, game_id, request_id, text,
                                            expected_epoch=memory_epoch)
        return view

    def _settle_prepared_turn(self, player_id: str, game_id: str, request_id: str,
                              player_text: str, item: GameListing, prepared: TurnSettlement,
                              memory_epoch: int) -> dict:
        billing = self.billing or self.billing_for_policy(prepared.policy)
        with self.telemetry.span(
            "billing-settlement",
            {"game_id": game_id, "request_id": request_id,
             "model_calls": len(prepared.usage), "pricing_version": prepared.policy.pricing_version},
            session_id=session_id_for_game(game_id, item.package_id),
        ) as span:
            billed = billing.settle_turn(player_id, game_id, request_id, player_text,
                                         prepared.response, prepared.usage, policy=prepared.policy)
            span.metric("story.credits_charged", billed["billing"]["charged_milli_points"] / 1000)
            span.metric("story.input_tokens", float(billed["billing"]["input_tokens"]))
            span.metric("story.output_tokens", float(billed["billing"]["output_tokens"]))
            self.memory_service.queue_input(player_id, game_id, request_id, player_text,
                                            expected_epoch=memory_epoch)
            return billed

    async def _recover_freeform(self, item: GameListing, package: ScenarioPackage,
                                game_id: str, text: str, turn_id: str,
                                mode: str) -> dict | None:
        committed = next(((kind, self.store.event_details(game_id, f"{turn_id}:{kind}"))
                          for kind in ("input", "query", "action")
                          if self.store.event_exists(game_id, f"{turn_id}:{kind}")), None)
        if committed is None:
            return None
        kind, details = committed
        if details is None or details.get("text") != text:
            raise ValueError("request ID already belongs to different input")
        history = self.store.player_inputs_for(game_id)
        if not history or history[-1].event_id != f"{turn_id}:{kind}":
            raise ValueError("a later turn has already started; resume the saved game")
        turn_engine = (self.factory.engine(item, package, game_id, overnight_requires_rest=True)
                 if package.story_blueprint is not None else self.factory.engine(item, package, game_id))
        await turn_engine.run_ready_work(game_id)
        snapshot = self.store.load(game_id)
        visible = [observation for observation in self.store.observations_for(game_id, "player")
                   if observation.event_id.startswith(f"{turn_id}:")]
        segments = tuple(segment_for_observation(self.store, game_id, item) for item in visible)
        body = "\n\n".join(item.body_text for item in segments) or "上次操作已提交，暂时没有新的可见变化。"
        guidance = (GuidanceResult((), "source_story") if package.story_blueprint else
                    await GuidanceAdvisor(self.store, package, telemetry=self.telemetry).advise(
                        game_id, snapshot, visible_text=body))
        single = turn_engine
        action_options = single.proposed_options(game_id, turn_id)
        clock = (StoryClock(package.ticks_per_day * item.turns_per_story_tick)
                 if package.ticks_per_day is not None else None)
        return turn_view(game_id, item.game_id, mode,
                          body, snapshot, guidance,
                          turn_id=turn_id, segments=segments,
                          presentation_mode=package.presentation_mode,
                          action_options=action_options, clock=clock,
                          status_fields=package.status_fields, time_unit=package.time_unit)
