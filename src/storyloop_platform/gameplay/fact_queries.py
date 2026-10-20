"""Authenticated, zero-model explicit fact query execution."""

from __future__ import annotations

import re

from storyloop_harness import FactQuery, FactQueryInput, GameStore, ScenarioPackage
from storyloop_harness.advanced import StoryClock

from storyloop_platform.gameplay.access import GameAccess, require_supported_save
from storyloop_platform.gameplay.factory import GameplayRuntime
from storyloop_platform.gameplay.locks import PlayerTurnLocks
from storyloop_platform.gameplay.presentation import package_for_mode, turn_view
from storyloop_platform.portal.fact_query_guards import require_no_ordinary_evidence
from storyloop_platform.portal.moderation import ScenarioModerationService
from storyloop_platform.portal.sql_repository import SQLPlayerRepository
from storyloop_platform.portal.turn_errors import TurnInputError, TurnRecoveryRequired
from storyloop_platform.runtime.guidance import GuidanceResult


class FactQueryService:
    def __init__(self, *, store: GameStore, accounts: SQLPlayerRepository,
                 game_access: GameAccess, moderation: ScenarioModerationService,
                 factory: GameplayRuntime, player_locks: PlayerTurnLocks) -> None:
        self.store = store
        self.accounts = accounts
        self.game_access = game_access
        self.moderation = moderation
        self.factory = factory
        self.player_locks = player_locks

    async def query(self, player_id: str, game_id: str, request_id: str,
                    fact_id: str, speaker_id: str) -> dict:
        async with self.player_locks.for_player(player_id):
            return await self._query_locked(player_id, game_id, request_id, fact_id, speaker_id)

    async def _query_locked(self, player_id: str, game_id: str, request_id: str,
                            fact_id: str, speaker_id: str) -> dict:
        record = self.accounts.get_save(player_id, game_id)
        if type(request_id) is not str or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", request_id) is None:
            raise TurnInputError("request_id must contain 1–64 URL-safe characters")
        if any(type(value) is not str or len(value) > 256 for value in (fact_id, speaker_id)):
            raise TurnInputError("fact_id and speaker_id must be explicit identifiers of at most 256 characters")
        try:
            query = FactQuery(fact_id=fact_id, speaker_id=speaker_id)
        except ValueError as error:
            raise TurnInputError(str(error)) from error
        require_supported_save(self.store, game_id)
        if self.moderation.preview_for(game_id, player_id) is not None:
            raise TurnInputError("fact queries require an ordinary freeform interactive save")
        item = self.game_access.verified_listing(record)
        mode = record.play_mode or item.mode
        raw_package = ScenarioPackage.load(item.package_path)
        if (mode != "freeform"
                or raw_package.presentation_mode != "interactive"
                or raw_package.story_blueprint is not None):
            raise TurnInputError("fact queries require an ordinary freeform interactive save")
        package = package_for_mode(raw_package, mode)
        snapshot = self.store.load(game_id)
        if "campaign" in snapshot.data:
            raise TurnInputError("fact queries require an ordinary freeform interactive save")
        require_no_ordinary_evidence(self.accounts.engine, game_id, request_id,
                                     committed_fact=False, same_id_only=True)
        cached = self.accounts.get_fact_query_response(game_id, request_id, fact_id, speaker_id)
        if cached is not None:
            return cached
        turn_id = "portal-" + request_id
        input_exists = self.store.event_exists(game_id, turn_id + ":input")
        details = self.store.event_details(game_id, turn_id + ":input") if input_exists else None
        if not input_exists:
            for kind in ("query", "action"):
                if self.store.event_exists(game_id, turn_id + ":" + kind):
                    raise TurnRecoveryRequired("请求 ID 已属于普通行动。")
        elif not isinstance(details, dict) or "fact_query" not in details:
            raise TurnRecoveryRequired("请求 ID 已属于普通行动。")
        committed_fact = input_exists
        require_no_ordinary_evidence(self.accounts.engine, game_id, request_id,
                                     committed_fact=committed_fact)
        if not committed_fact:
            progress = snapshot.data.get("story_progress", {})
            if isinstance(progress, dict) and progress.get("complete") is True:
                raise TurnInputError("this story is complete")
            actors = snapshot.data.get("actors", {})
            player = actors.get("player") if isinstance(actors, dict) else None
            speaker = actors.get(speaker_id) if isinstance(actors, dict) else None
            if (speaker_id == "player" or speaker_id not in dict(package.actor_cards)
                    or not isinstance(player, dict) or not isinstance(speaker, dict)
                    or not isinstance(player.get("location"), str) or not player["location"]
                    or speaker.get("location") != player["location"]):
                raise TurnInputError("fact query requires a reachable NPC speaker")
        engine = self.factory.fact_query_engine(item, package, game_id)
        outcome = await engine.run_fact_query(FactQueryInput(
            game_id=game_id, query=query, turn_id=turn_id,
            scenario_version=package.version))
        snapshot = self.store.load(game_id)
        clock = (StoryClock(package.ticks_per_day * item.turns_per_story_tick)
                 if package.ticks_per_day is not None else None)
        view = turn_view(game_id, item.game_id, mode, outcome.narration, snapshot,
                         GuidanceResult((), "fact_query"), turn_id=turn_id,
                         segments=outcome.segments, presentation_mode=package.presentation_mode,
                         clock=clock, status_fields=package.status_fields,
                         time_unit=package.time_unit)
        self.accounts.store_fact_query_response(game_id, request_id, fact_id, speaker_id, view)
        return view
