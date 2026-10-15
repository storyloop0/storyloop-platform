"""Turn execution, recovery, and immutable usage settlement."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import ExitStack, nullcontext
from hashlib import sha256
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
from storyloop_platform.gameplay.turn_recovery import (
    RecoveryQualification, inspect_scene, valid_cached_view, validate_scene_usage,
)
from storyloop_platform.gameplay.scene_recovery import SceneRecoveryPlan, SceneRecoveryRuntime
from storyloop_platform.memory.service import PlayerMemoryService
from storyloop_platform.portal.billing import collect_usage, BillingPolicy
from storyloop_platform.portal.catalog import GameListing
from storyloop_platform.portal.moderation import ScenarioModerationService
from storyloop_platform.portal.model_execution_audits import SQLModelExecutionAudits
from storyloop_platform.portal.sql_billing import SQLBillingRepository
from storyloop_platform.portal.sql_repository import SQLPlayerRepository
from storyloop_platform.portal.turn_errors import TurnInputError, TurnRecoveryRequired
from storyloop_platform.portal.turn_settlements import SQLTurnSettlements, TurnSettlement
from storyloop_platform.portal.turn_recovery_contexts import SQLTurnRecoveryContexts, TurnRecoveryContextError
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
                 audits: SQLModelExecutionAudits | None = None,
                 recovery_contexts: SQLTurnRecoveryContexts | None = None) -> None:
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
        self.recovery_contexts = recovery_contexts or SQLTurnRecoveryContexts(accounts.engine)

    def recovery_status(self, player_id: str, game_id: str,
                        request_id: str, input_hash: str) -> dict:
        record = self.accounts.get_save(player_id, game_id)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", request_id) or not re.fullmatch(r"[0-9a-f]{64}", input_hash):
            raise ValueError("invalid recovery status parameters")
        try:
            require_supported_save(self.store, game_id)
            item = self.game_access.verified_listing(record)
            receipt = self.accounts.get_turn_response_by_hash(game_id, request_id, input_hash)
            if receipt is not None:
                return RecoveryQualification("RECEIPT_READY" if valid_cached_view(receipt)
                                             else "EVIDENCE_INVALID").status(request_id)
            prepared = self.settlements.get_by_hash(game_id, request_id, input_hash)
            if prepared is not None:
                self._qualify_prepared(player_id, game_id, request_id, input_hash, prepared)
                return RecoveryQualification("PREPARED_READY").status(request_id)
            context = self.recovery_contexts.get(game_id, request_id)
            turn_id = "portal-" + request_id
            if context is None:
                if self.store.generated_scene_snapshot(game_id, turn_id) is not None or any(
                    self.store.event_exists(game_id, f"{turn_id}:{kind}")
                    for kind in ("input", "query", "action", "continue", "choice", "advance", "completed")
                ):
                    return RecoveryQualification("LEGACY_REQUEST").status(request_id)
                return RecoveryQualification("NO_SNAPSHOT").status(request_id)
            mode = record.play_mode or item.mode
            if (mode != "freeform" or self.moderation.preview_for(game_id, player_id) is not None):
                return RecoveryQualification("UNSUPPORTED").status(request_id)
            package = package_for_mode(ScenarioPackage.load(item.package_path), mode)
            if package.story_blueprint is not None or package.presentation_mode != "interactive":
                return RecoveryQualification("UNSUPPORTED").status(request_id)
            return inspect_scene(store=self.store, factory=self.factory,
                                 contexts=self.recovery_contexts, game_id=game_id,
                                 request_id=request_id, input_hash=input_hash,
                                 item=item, package=package, context=context).status(request_id)
        except ValueError as error:
            if str(error) == "request ID already belongs to different input":
                return RecoveryQualification("INPUT_MISMATCH").status(request_id)
            if isinstance(error, TurnRecoveryContextError):
                return RecoveryQualification("EVIDENCE_INVALID").status(request_id)
            return RecoveryQualification("EVIDENCE_INVALID").status(request_id)

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
            self._qualify_prepared(player_id, game_id, request_id,
                                   sha256(text.encode()).hexdigest(), prepared)
            return self._settle_prepared_turn(player_id, game_id, request_id, text, item,
                                              prepared, memory_epoch)
        package = package_for_mode(ScenarioPackage.load(item.package_path), mode)
        if (not preview and mode == "freeform" and package.story_blueprint is None
                and package.presentation_mode == "interactive"
                and isinstance(self.factory, SceneRecoveryRuntime)):
            plan = self.factory.scene_recovery_plan(item, package, game_id)
            if plan is not None:
                return await self._controlled_turn(player_id, game_id, text, request_id,
                                                   progress, item, package, plan, memory_epoch)
        turn_id = f"portal-{request_id}"
        committed_same_turn = any(self.store.event_exists(game_id, f"{turn_id}:{kind}")
                                  for kind in ("input", "query", "action", "continue",
                                               "choice", "advance", "completed"))
        if committed_same_turn and not preview:
            # Until model-call journaling exists, earlier interrupted turns cannot
            # be reconstructed with a fresh (empty) usage collector and billed as zero.
            raise TurnRecoveryRequired("行动已提交，但计量恢复记录不完整，请联系管理员恢复此回合。")
        self._require_story_open(game_id, committed_same_turn)
        if preview and self.moderation.preview_turn_count(game_id) >= 12:
            raise ValueError("review preview has reached its 12-turn limit")
        if self.billing is not None and not preview:
            self.billing.require_credit(player_id)
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
                                              prepared, memory_epoch, queue_memory=True)
        self.accounts.store_turn_response(game_id, request_id, text, view)
        if not preview:
            self.memory_service.queue_input(player_id, game_id, request_id, text,
                                            expected_epoch=memory_epoch)
        return view

    def _qualify_prepared(self, player_id: str, game_id: str, request_id: str,
                          input_hash: str, prepared: TurnSettlement) -> None:
        """Check finished settlement evidence without consulting today's generation setup."""
        if not valid_cached_view(prepared.response):
            raise TurnRecoveryRequired("保存的回合响应无效，请联系管理员恢复。")
        try:
            context = self.recovery_contexts.get(game_id, request_id)
            if context is None:
                return  # Older prepared rows have no controlled-scene context.
            if (context.player_id != player_id or context.game_id != game_id or
                    context.request_id != request_id or context.turn_id != "portal-" + request_id or
                    context.input_hash != input_hash or not context.billing_enabled or
                    prepared.policy != context.policy()):
                raise ValueError("prepared context does not match original billing")
            scene = self.store.generated_scene_snapshot(game_id, context.turn_id)
            if scene is None:
                raise ValueError("prepared scene is missing")
            metadata = scene.metadata()
            if (scene.game_id != game_id or scene.turn_id != context.turn_id or
                    metadata["input_hash"] != input_hash or
                    metadata["base_version"] != context.base_version or
                    metadata["base_tick"] != context.base_tick or
                    metadata["base_state_hash"] != context.base_state_hash or
                    metadata["usage_coverage"] != "reported" or
                    tuple(prepared.usage) != scene.success_usage()):
                raise ValueError("prepared scene does not match original usage")
            context.policy().price_milli_points(prepared.usage)
        except ValueError as error:
            raise TurnRecoveryRequired("保存的结算证据无效，请联系管理员恢复。") from error

    async def _controlled_turn(self, player_id: str, game_id: str, text: str,
                               request_id: str, progress: TurnProgress | None,
                               item: GameListing, package: ScenarioPackage,
                               plan: SceneRecoveryPlan, memory_epoch: int) -> dict:
        input_hash = sha256(text.encode()).hexdigest()
        context = self.recovery_contexts.get(game_id, request_id)
        turn_id = "portal-" + request_id
        try:
            existing_scene = self.store.generated_scene_snapshot(game_id, turn_id)
        except ValueError as error:
            raise TurnRecoveryRequired("保存的场景证据无效，请联系管理员恢复。") from error
        if context is None and (existing_scene is not None or any(
                self.store.event_exists(game_id, f"{turn_id}:{kind}") for kind in
                ("input", "query", "action", "continue", "choice", "advance", "completed"))):
            raise TurnRecoveryRequired("行动已有旧版恢复记录，无法安全恢复。")
        if existing_scene is None:
            committed_same_turn = any(self.store.event_exists(game_id, f"{turn_id}:{kind}")
                                      for kind in ("input", "query", "action", "continue",
                                                   "choice", "advance", "completed"))
            self._require_story_open(game_id, committed_same_turn)
        if context is not None:
            try:
                qualified = inspect_scene(store=self.store, factory=self.factory,
                                          contexts=self.recovery_contexts, game_id=game_id,
                                          request_id=request_id, input_hash=input_hash,
                                          item=item, package=package, context=context,
                                          plan=plan)
            except ValueError as error:
                raise TurnRecoveryRequired("行动恢复证据无效，请联系管理员恢复。") from error
            if qualified.reason not in ("SCENE_READY", "NO_SNAPSHOT"):
                raise TurnRecoveryRequired("行动恢复证据不完整，请联系管理员恢复此回合。")
            if qualified.reason == "NO_SNAPSHOT" and existing_scene is not None:
                raise TurnRecoveryRequired("行动恢复证据不完整，请联系管理员恢复此回合。")
        billing_enabled = context.billing_enabled if context is not None else self.billing is not None
        policy = context.policy() if context is not None else (self.billing.policy if billing_enabled else None)
        if existing_scene is None and billing_enabled:
            self.billing_for_policy(policy).require_credit(player_id)
        preferences = (() if existing_scene is not None else
                       await self.memory_service.preferences_for(player_id))
        created = False
        with ExitStack() as finalizers:
            async with execution_budget_scope(ExecutionBudget(
                    plan.execution_max_requests, plan.execution_timeout_seconds)):
                context, created = self.recovery_contexts.freeze(
                    player_id=player_id, game_id=game_id, request_id=request_id,
                    input_hash=input_hash, generation_identity=plan.generation_identity,
                    billing_enabled=billing_enabled, policy=policy)
                finalizers.enter_context(attempt_audit_scope(
                    self.audits, player_id=player_id, game_id=game_id,
                    request_id=request_id, player_text=text, policy=context.policy()))
                with collect_usage() as meter, player_preferences_scope(preferences):
                    recovery_only = existing_scene is not None
                    engine = self.factory.scene_recovery_engine(
                        item, package, game_id, plan=plan, recovery_only=recovery_only)
                    outcome = await engine.run_turn(
                        TurnInput(game_id, text, turn_id, package.version), progress=progress)
                    try:
                        inspection = engine.inspect_generated_scene_recovery(
                            TurnInput(game_id, text, turn_id, package.version))
                    except ValueError as error:
                        raise TurnRecoveryRequired("保存的场景证据无效，请联系管理员恢复。") from error
                    if inspection is None:
                        raise TurnRecoveryRequired("成功场景快照不可用，无法结算。")
                    if (inspection.snapshot.metadata()["base_version"] != context.base_version or
                            inspection.snapshot.metadata()["base_tick"] != context.base_tick or
                            inspection.snapshot.metadata()["base_state_hash"] != context.base_state_hash):
                        raise TurnRecoveryRequired("场景基线与恢复记录不匹配。")
                    try:
                        scene_usage = validate_scene_usage(inspection, context)
                    except ValueError as error:
                        raise TurnRecoveryRequired("场景计量证据不完整，无法结算。") from error
                    if (outcome.model_usage if recovery_only else
                            tuple(outcome.model_usage) != tuple(scene_usage)):
                        raise TurnRecoveryRequired("场景内出现未覆盖的模型计量，无法结算。")
                    snapshot = self.store.load(game_id)
                    base_ids = set(context.base_visible_observation_ids)
                    visible = [observation for observation in self.store.observations_for(game_id, "player")
                               if observation.observation_id not in base_ids]
                    segments = tuple(segment_for_observation(self.store, game_id, observation)
                                     for observation in visible)
                    if not segments:
                        segments = outcome.segments
                    body = "\n\n".join(part.body_text for part in segments)
                    await emit(progress, "preview", body=body,
                               segments=[part.to_dict() for part in segments])
                    await emit(progress, "stage", stage="guidance")
                    guidance = await GuidanceAdvisor(self.store, package, telemetry=self.telemetry).advise(
                        game_id, snapshot, visible_text=body)
                    options = engine.proposed_options(game_id, turn_id)
                    clock = (StoryClock(package.ticks_per_day * plan.turns_per_story_tick)
                             if package.ticks_per_day is not None else None)
                    view = turn_view(game_id, item.game_id, "freeform", body, snapshot, guidance,
                                     turn_id=turn_id, segments=segments,
                                     presentation_mode=package.presentation_mode,
                                     action_options=options, clock=clock,
                                     status_fields=package.status_fields,
                                     time_unit=package.time_unit)
                    # The scene generator's nested collector feeds the snapshot. Any
                    # usage visible to this outer collector is an uncovered call,
                    # even if its model and token counts happen to match the scene.
                    if meter.records:
                        raise TurnRecoveryRequired("场景外出现未覆盖的模型计量，无法结算。")
        if context.billing_enabled:
            prepared = self.settlements.prepare(game_id, request_id, text, view,
                                                 scene_usage, context.policy())
            return self._settle_prepared_turn(player_id, game_id, request_id, text, item,
                                              prepared, memory_epoch, queue_memory=created)
        self.accounts.store_turn_response(game_id, request_id, text, view)
        if created:
            self.memory_service.queue_input(player_id, game_id, request_id, text,
                                            expected_epoch=memory_epoch)
        return view

    def _require_story_open(self, game_id: str, committed_same_turn: bool) -> None:
        progress_state = self.store.load(game_id).data.get("story_progress", {})
        if (isinstance(progress_state, dict) and progress_state.get("complete") is True
                and not committed_same_turn):
            raise TurnInputError("this story is complete")

    def _settle_prepared_turn(self, player_id: str, game_id: str, request_id: str,
                              player_text: str, item: GameListing, prepared: TurnSettlement,
                              memory_epoch: int, *, queue_memory: bool = False) -> dict:
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
            if queue_memory:
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
