"""Production composition root and sole owner of portal resources."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

from storyloop_harness import ScenarioPackage
from storyloop_harness.generation import ExecutionBudget
from storyloop_platform.adapters.telemetry import configured_telemetry
from storyloop_platform.config import ModelFactory, PlatformResources, PlatformSettings
from storyloop_platform.gameplay.access import GameAccess
from storyloop_platform.gameplay.factory import RuntimeFactory, RuntimeFactoryBuilder
from storyloop_platform.gameplay.locks import PlayerTurnLocks
from storyloop_platform.gameplay.service import GameplayService
from storyloop_platform.gameplay.turns import TurnExecutionService
from storyloop_platform.generators.prologue_generator import ModelPrologueGenerator
from storyloop_platform.lifecycle import OperationSupervisor
from storyloop_platform.memory.jobs import SQLPlayerMemoryJobs
from storyloop_platform.memory.providers import Mem0PlayerMemory
from storyloop_platform.memory.service import PlayerMemoryService
from storyloop_platform.portal.access import AccessService
from storyloop_platform.portal.billing import BillingPolicy
from storyloop_platform.portal.catalog import GameCatalog
from storyloop_platform.portal.invitations import InvitationService
from storyloop_platform.portal.moderation import ScenarioModerationService
from storyloop_platform.portal.model_execution_audits import SQLModelExecutionAudits
from storyloop_platform.portal.save_generation_settings import SQLSaveGenerationSettings
from storyloop_platform.portal.service import PlayerPortal
from storyloop_platform.portal.sql_billing import SQLBillingRepository
from storyloop_platform.portal.sql_repository import SQLPlayerRepository
from storyloop_platform.portal.turn_settlements import SQLTurnSettlements
from storyloop_platform.portal.turn_recovery_contexts import SQLTurnRecoveryContexts
from storyloop_platform.portal.user_scenarios import UserScenarioService
from storyloop_platform.runtime.campaign import CampaignProgram


PrologueGenerator = Callable[[ScenarioPackage, CampaignProgram | None, str, str], Awaitable[str]]


def build_portal(
    catalog_path: str | Path | None, settings: PlatformSettings,
    db_path: str | None = None, *, catalog: GameCatalog | None = None,
    prologue_generator: PrologueGenerator | None = None,
    runtime_factory_builder: RuntimeFactoryBuilder | None = None,
) -> PlayerPortal:
    if catalog is None:
        if catalog_path is None:
            raise ValueError("catalog path or catalog provider is required")
        catalog = GameCatalog.load(catalog_path)
    resources = PlatformResources(settings)
    closers: list[Callable[[], None]] = []
    closed = False
    operations = OperationSupervisor()
    shutdown_task: asyncio.Task[None] | None = None

    def close_resources() -> None:
        nonlocal closed
        if closed:
            return
        closed = True
        failures: list[BaseException] = []
        for close in closers:
            try:
                close()
            except BaseException as error:
                failures.append(error)
        if failures:
            raise BaseExceptionGroup("portal resource cleanup failed", failures)

    try:
        telemetry = configured_telemetry()
        closers.append(telemetry.flush)
        models = ModelFactory(settings, telemetry=telemetry)
        resources.allowed_hosts()
        if settings.environment == "online":
            models.require_credentials()
        selected_db = db_path or settings.storage.path
        upload_dir = os.environ.get("STORY_UPLOAD_DIR")
        if not upload_dir and settings.environment == "online":
            raise ValueError("STORY_UPLOAD_DIR is required in online mode")
        if not upload_dir:
            upload_dir = str(Path(selected_db).resolve().parent / "uploaded-scenarios")
        engine = resources.create_database(db_path)
        closers.append(engine.dispose)
        store = resources.create_store(engine=engine)
        accounts = SQLPlayerRepository(engine)
        settlements = SQLTurnSettlements(engine)
        generation_settings = SQLSaveGenerationSettings(engine)
        access = AccessService(engine)
        invitations = InvitationService(engine, access)
        provider = (Mem0PlayerMemory(models, resources.player_memory_path())
                    if settings.player_memory.driver == "mem0" else None)
        if provider is not None:
            closers.insert(1, provider.close)
        memory_service = PlayerMemoryService(provider, SQLPlayerMemoryJobs(engine),
                                             resources.player_memory_enabled(), telemetry, operations,
                                             generated_preference_max_age_days=(
                                                 settings.player_memory.generated_preference_max_age_days))
        if provider is not None:
            closers[1] = memory_service.close
        else:
            closers.insert(1, memory_service.close)

        async def generate_prologue(package: ScenarioPackage, program: CampaignProgram | None,
                                    title: str, summary: str) -> str:
            task = "prologue" if "prologue" in settings.routes else "narration"
            model = models.create_model(task)
            return await ModelPrologueGenerator(model, telemetry).generate(
                package, program, title, summary)

        user_scenarios = UserScenarioService(
            engine, upload_dir, operations=operations,
            prologue_generator=prologue_generator or generate_prologue)
        moderation = ScenarioModerationService(engine, user_scenarios, access)

        def billing_for_policy(policy: BillingPolicy) -> SQLBillingRepository:
            return SQLBillingRepository(engine, policy, accounts)

        policy = resources.billing_policy
        billing = billing_for_policy(policy) if policy is not None else None
        game_access = GameAccess(catalog=catalog, user_scenarios=user_scenarios,
                                 moderation=moderation)
        player_locks = PlayerTurnLocks()
        factory = (runtime_factory_builder or RuntimeFactory)(
            settings=settings, store=store, accounts=accounts,
            generation_settings=generation_settings, models=models, telemetry=telemetry)
        gameplay = GameplayService(
            store=store, accounts=accounts, game_access=game_access, moderation=moderation,
            generation_settings=generation_settings, factory=factory,
            player_locks=player_locks, telemetry=telemetry)
        turns = TurnExecutionService(
            store=store, accounts=accounts, game_access=game_access, moderation=moderation,
            settlements=settlements, billing=billing, billing_for_policy=billing_for_policy,
            memory_service=memory_service, factory=factory, player_locks=player_locks,
            telemetry=telemetry, audits=SQLModelExecutionAudits(engine),
            recovery_contexts=SQLTurnRecoveryContexts(engine),
            execution_budget=ExecutionBudget(settings.runtime.turn_max_requests,
                                             settings.runtime.turn_timeout_seconds))

        def guarded_close() -> None:
            operations.close()
            close_resources()

        async def shutdown_resources(grace_timeout: float, cancel_timeout: float) -> None:
            nonlocal shutdown_task
            operations.stop_admission()
            memory_service.request_stop()
            if shutdown_task is None:
                async def drain_and_close() -> None:
                    await operations.shutdown(grace_timeout=grace_timeout, cancel_timeout=cancel_timeout)
                    guarded_close()
                shutdown_task = asyncio.create_task(drain_and_close(), name="portal-shutdown")

                def completed(task: asyncio.Task[None]) -> None:
                    if not task.cancelled() and (error := task.exception()) is not None:
                        logging.getLogger(__name__).error("portal shutdown failed", exc_info=error)
                shutdown_task.add_done_callback(completed)
            await asyncio.shield(shutdown_task)

        return PlayerPortal(
            settings=settings, resources=resources, db_path=selected_db, accounts=accounts,
            access=access, invitations=invitations, user_scenarios=user_scenarios,
            moderation=moderation, billing=billing, memory_service=memory_service,
            gameplay=gameplay, turns=turns, operations=operations,
            close_resources=guarded_close, shutdown_resources=shutdown_resources)
    except BaseException as error:
        try:
            close_resources()
        except BaseException as cleanup_error:
            error.add_note(f"Partial construction cleanup failed: {cleanup_error}")
        raise
