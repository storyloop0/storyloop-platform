"""Save lifecycle and gameplay queries."""

from __future__ import annotations

import math
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from storyloop_harness import GameStore, ScenarioPackage
from storyloop_harness.advanced import Observation, Snapshot, WorldEvent, StoryClock, player_preferences_scope, render_prepared_opening
from storyloop_harness.generation import ActionOption
from storyloop_harness.telemetry import Telemetry

from storyloop_platform.gameplay.access import GameAccess, require_supported_save
from storyloop_platform.gameplay.factory import GameplayRuntime
from storyloop_platform.gameplay.locks import PlayerTurnLocks
from storyloop_platform.gameplay.presentation import package_for_mode, turn_view
from storyloop_platform.portal.catalog import GameListing
from storyloop_platform.portal.character_cards import shared_actor_memories
from storyloop_platform.portal.moderation import ScenarioModerationService
from storyloop_platform.portal.save_generation_settings import SaveGenerationSettings, SQLSaveGenerationSettings
from storyloop_platform.portal.sql_repository import SQLPlayerRepository
from storyloop_platform.runtime.campaign import CampaignProgram
from storyloop_platform.runtime.guidance import GuidanceAdvisor, GuidanceResult


class GameplayService:
    """Save lifecycle and queries; SQL collaborators must share one Engine."""

    def __init__(self, *, store: GameStore, accounts: SQLPlayerRepository,
                 game_access: GameAccess, moderation: ScenarioModerationService,
                 generation_settings: SQLSaveGenerationSettings, factory: GameplayRuntime,
                 player_locks: PlayerTurnLocks, telemetry: Telemetry) -> None:
        self.store = store
        self.accounts = accounts
        self.game_access = game_access
        self.moderation = moderation
        self.generation_settings = generation_settings
        self.factory = factory
        self.player_locks = player_locks
        self.telemetry = telemetry

    def games(self, player_id: str) -> list[dict]:
        listings = self.game_access.listings(player_id)
        result = []
        for item in listings:
            blueprint = ScenarioPackage.load(item.package_path).story_blueprint
            result.append({
                "id": item.game_id, "title": item.title, "mode": item.mode,
                "play_modes": list(item.supported_play_modes),
                "story_setup": blueprint.public_setup() if blueprint else None,
                "summary": item.summary, "genre": item.genre, "theme": item.theme,
                "cover_url": (f"/v1/catalog/{quote(item.game_id, safe='')}/artwork/cover"
                              if item.cover_art else None),
            })
        return result

    def cover_artwork(self, player_id: str, catalog_id: str) -> Path:
        item = self.game_access.listing_for(player_id, catalog_id)
        if not item.cover_art:
            raise KeyError("cover not found")
        return self._artwork_path(item, item.cover_art)

    def player_card(self, player_id: str, game_id: str) -> dict:
        self.accounts.get_save(player_id, game_id)
        profile = self.store.load(game_id).data.get("player_profile")
        if not isinstance(profile, dict):
            return {"name": "", "fields": []}
        name = profile.get("name")
        name = name.strip()[:80] if isinstance(name, str) else ""
        fields = []
        for key, value in profile.items():
            if (len(fields) >= 24 or not isinstance(key, str) or key == "name"
                    or key.startswith("_") or not 0 < len(key.strip()) <= 64
                    or not isinstance(value, str) or not value.strip()):
                continue
            fields.append({"key": key.strip(), "value": value.strip()[:1000]})
        return {"name": name, "fields": fields}

    def cast(self, player_id: str, game_id: str) -> list[dict]:
        record = self.accounts.get_save(player_id, game_id)
        item = self.game_access.verified_listing(record)
        package = ScenarioPackage.load(item.package_path)
        snapshot = self.store.load(game_id)
        generated = snapshot.data.get("actor_profiles", {})
        if not isinstance(generated, dict):
            generated = {}
        def visible_name(actor_id: str) -> str:
            profile = generated.get(actor_id)
            return (profile.get("name") if isinstance(profile, dict)
                    and isinstance(profile.get("name"), str) else package.actor_names[actor_id])
        knowledge = snapshot.data.get("player_knowledge")
        if package.story_blueprint is not None and isinstance(knowledge, dict):
            named = knowledge.get("named_actor_ids", [])
            visible = ({actor_id for actor_id in named if actor_id in package.actor_names}
                       if isinstance(named, list) else set())
        else:
            visible = self._legacy_visible_cast(game_id, item, package, visible_name, snapshot)
        return [{"id": actor_id, "name": visible_name(actor_id),
                 "portrait_url": (f"/v1/saves/{game_id}/cast/{quote(actor_id, safe='')}/portrait"
                                  if actor_id in (item.portrait_art or {})
                                  else None)}
                for actor_id, _ in package.actor_cards
                if actor_id in visible]

    def character_detail(self, player_id: str, game_id: str, actor_id: str) -> dict:
        member = next((actor for actor in self.cast(player_id, game_id) if actor["id"] == actor_id), None)
        if member is None:
            raise KeyError("character not available")
        record = self.accounts.get_save(player_id, game_id)
        item = self.game_access.verified_listing(record)
        state = self.store.load(game_id).data
        campaign = state.get("campaign")
        affinity = (campaign.get("affinity", {}).get(actor_id)
                    if isinstance(campaign, dict) and isinstance(campaign.get("affinity"), dict)
                    else None)
        count, memories = shared_actor_memories(self.store, game_id, actor_id)
        return {**member,
                "profile": (state.get("actor_profiles", {}).get(actor_id, {}).get("public_profile", "")
                            if isinstance(state.get("actor_profiles"), dict)
                            else (item.public_profiles or {}).get(actor_id, "")),
                "affinity": affinity if type(affinity) is int else None,
                "shared_event_count": count,
                "memories": memories}

    def portrait_artwork(self, player_id: str, game_id: str, actor_id: str) -> Path:
        visible = {actor["id"] for actor in self.cast(player_id, game_id)}
        if actor_id not in visible:
            raise KeyError("portrait not available")
        record = self.accounts.get_save(player_id, game_id)
        item = self.game_access.verified_listing(record)
        art_path = (item.portrait_art or {}).get(actor_id)
        if art_path is None:
            raise KeyError("portrait not available")
        return self._artwork_path(item, art_path)

    def history(self, player_id: str, game_id: str) -> dict:
        record = self.accounts.get_save(player_id, game_id)
        item = self.game_access.verified_listing(record)
        intro = self.accounts.get_intro(game_id)
        if intro is None:
            package = ScenarioPackage.load(item.package_path)
            intro = turn_view(game_id, item.game_id, record.play_mode or item.mode, "", self.store.load(game_id),
                               guidance=GuidanceResult((), "history_fallback"),
                               opening=package.opening, status_fields=package.status_fields,
                               time_unit=package.time_unit)
        return {"game_id": game_id, "intro": intro, "turns": self.accounts.list_turns(game_id)}

    def saves(self, player_id: str) -> list[dict]:
        result = []
        for record in self.accounts.list_saves(player_id):
            try:
                item = self.game_access.verified_listing(record)
            except (KeyError, ValueError, PermissionError, FileNotFoundError):
                # Authentication was resolved above; this is access to one save's content.
                item = None
            snapshot = self.store.load(record.game_id)
            campaign = snapshot.data.get("campaign")
            result.append({"game_id": record.game_id, "catalog_id": record.catalog_id,
                           "title": item.title if item else record.catalog_id,
                           "mode": (record.play_mode or item.mode) if item else record.play_mode,
                           "available": item is not None,
                           "unavailable_reason": None if item else "剧本已不可用，或你已失去访问权限。",
                           "tick": snapshot.tick,
                           "day": campaign.get("day") if isinstance(campaign, dict) else None,
                           "complete": bool(campaign.get("ending")) if isinstance(campaign, dict) else False})
        return result

    def get_save_settings(self, player_id: str, game_id: str) -> dict:
        self.accounts.get_save(player_id, game_id)
        defaults = self.factory.default_generation_settings()
        settings = self.factory.generation_settings(game_id)
        return {"temperature": settings.temperature,
                "context_window_tokens": settings.context_window_tokens,
                "default_temperature": defaults.temperature,
                "default_context_window_tokens": defaults.context_window_tokens,
                "max_context_window_tokens": defaults.context_window_tokens}

    async def set_save_settings(self, player_id: str, game_id: str,
                                temperature: float, context_window_tokens: int) -> dict:
        async with self.player_locks.for_player(player_id):
            self.accounts.get_save(player_id, game_id)
            max_window = self.factory.default_generation_settings().context_window_tokens
            if (isinstance(temperature, bool) or not isinstance(temperature, (int, float))
                    or not math.isfinite(temperature) or not 0 <= temperature <= 1.9):
                raise ValueError("temperature must be between 0 and 1.9")
            if (type(context_window_tokens) is not int or
                    not 8192 <= context_window_tokens <= max_window):
                raise ValueError(f"context window must be between 8192 and {max_window} tokens")
            previous = self.factory.generation_settings(game_id)
            settings = SaveGenerationSettings(float(temperature), context_window_tokens)
            self.factory.context_budget(settings)
            self.generation_settings.put(game_id, settings, previous)
            self.factory.invalidate(game_id)
        return self.get_save_settings(player_id, game_id)

    async def create_save(self, player_id: str, catalog_id: str,
                          play_mode: str | None = None,
                          story_setup: dict[str, str] | None = None) -> dict:
        item = self.game_access.listing_for(player_id, catalog_id)
        if item.retired:
            raise ValueError("this game is no longer available for new saves")
        selected_mode = play_mode or item.mode
        if selected_mode not in item.supported_play_modes:
            raise ValueError("this game does not support the selected play mode")
        package = package_for_mode(ScenarioPackage.load(item.package_path), selected_mode)
        game_id = uuid4().hex
        if package.story_blueprint is not None:
            result = await self._seed_source_save(item, package, game_id,
                                                  selected_mode, story_setup)
            self.accounts.create_save(player_id, item.game_id, game_id,
                                      item.package_id, item.package_version, item.fingerprint,
                                      selected_mode)
            self.accounts.store_intro(game_id, result)
            return result
        self.factory.require_credentials()
        package.seed_game(self.store, game_id, include_campaign=selected_mode == "campaign")
        self.accounts.create_save(player_id, item.game_id, game_id,
                                  item.package_id, item.package_version, item.fingerprint,
                                  selected_mode)
        result = await self._open_save(item, package, game_id, opening=package.opening,
                                       play_mode=selected_mode)
        self.accounts.store_intro(game_id, result)
        return result

    async def resume_save(self, player_id: str, game_id: str) -> dict:
        async with self.player_locks.for_player(player_id):
            return await self._resume_save_for_player(player_id, game_id)

    async def create_review_preview(self, player_id: str, submission_id: str) -> dict:
        reviewer_id = player_id
        item = self.moderation.preview_listing(reviewer_id, submission_id)
        package = package_for_mode(ScenarioPackage.load(item.package_path), item.mode)
        game_id = uuid4().hex
        if package.story_blueprint is not None:
            with player_preferences_scope(()):
                result = await self._seed_source_save(item, package, game_id,
                                                      item.mode, None)
        else:
            package.seed_game(self.store, game_id)
            with player_preferences_scope(()):
                result = await self._open_save(item, package, game_id, opening=package.opening)
        self.accounts.create_save(reviewer_id, item.game_id, game_id,
                                  item.package_id, item.package_version, item.fingerprint,
                                  item.mode)
        self.moderation.record_preview(game_id, submission_id, reviewer_id)
        self.accounts.store_intro(game_id, result)
        return result

    @staticmethod
    def _artwork_path(item: GameListing, relative_path: str) -> Path:
        root = item.package_path.resolve()
        path = (root / relative_path).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise KeyError("artwork not found")
        return path

    def _legacy_visible_cast(self, game_id: str, item: GameListing,
                             package: ScenarioPackage, visible_name,
                             snapshot: Snapshot) -> set[str]:
        visible: set[str] = set()
        campaign = snapshot.data.get("campaign")
        if isinstance(campaign, dict):
            met = campaign.get("met", {})
            if isinstance(met, dict):
                visible.update(actor_id for actor_id, known in met.items() if known is True)
            cursor = campaign.get("cursor", 0)
            if type(cursor) is int and cursor > 0:
                program = CampaignProgram.load(item.package_path / "campaign.json")
                for step in program.steps[:cursor]:
                    if step["kind"] == "scene":
                        visible.update(actor_id for actor_id, name in package.actor_names.items()
                                       if name in step["text"])
        for observation in self.store.observations_for(game_id, "player"):
            if observation.channel == "dialogue":
                speaker_id = (self.store.event_details(game_id, observation.event_id) or {}).get("speaker_id")
                if isinstance(speaker_id, str):
                    visible.add(speaker_id)
        for turn in self.accounts.list_turns(game_id):
            for segment in turn["response"].get("segments", ()):
                if segment.get("kind") == "dialogue" and isinstance(segment.get("speaker_id"), str):
                    visible.add(segment["speaker_id"])
        if package.story_blueprint is not None:
            intro = self.accounts.get_intro(game_id) or {}
            passages = [str(intro.get("opening", ""))]
            passages.extend(str(turn["response"].get("body", ""))
                           for turn in self.accounts.list_turns(game_id))
            visible.update(actor_id for actor_id, _ in package.actor_cards
                           if any(visible_name(actor_id) in passage for passage in passages))
        return visible

    async def _seed_source_save(self, item: GameListing, package: ScenarioPackage,
                                game_id: str, selected_mode: str,
                                story_setup: dict[str, str] | None) -> dict:
        blueprint = package.story_blueprint
        if blueprint is None:
            raise ValueError("source-driven save requires a story blueprint")
        setup = blueprint.setup.resolve(story_setup)
        opening = render_prepared_opening(package, setup)
        package.seed_game(self.store, game_id, include_campaign=False,
                          setup_state=opening.state)
        seeded = self.store.load(game_id)
        self.store.commit(game_id, seeded.version,
                          WorldEvent("opening:generated", "story_opening", "system",
                                     None, seeded.tick, (), {"source": "story_blueprint"}),
                          (Observation("opening:player", "opening:generated", "player",
                                       "scene", opening.prose, seeded.tick),), ())
        clock = (StoryClock(package.ticks_per_day * item.turns_per_story_tick)
                 if package.ticks_per_day is not None else None)
        return turn_view(
            game_id, item.game_id, selected_mode, "", self.store.load(game_id),
            GuidanceResult((), "opening"), opening=opening.prose,
            presentation_mode=package.presentation_mode,
            action_options=tuple(ActionOption.model_validate(option.model_dump())
                                 for option in opening.options),
            clock=clock, status_fields=package.status_fields,
            time_unit=package.time_unit,
        )

    async def _resume_save_for_player(self, player_id: str, game_id: str) -> dict:
        record = self.accounts.get_save(player_id, game_id)
        require_supported_save(self.store, game_id)
        item = self.game_access.verified_listing(record)
        latest = self.accounts.latest_turn_response(game_id)
        snapshot = self.store.load(game_id)
        if latest is None:
            intro = self.accounts.get_intro(game_id)
            if (intro is not None and snapshot.version == intro.get("state_version")
                    and not self.store.ready_work(game_id, snapshot.tick)
                    and ((record.play_mode or item.mode) != "campaign" or "interaction" in intro)):
                return {**intro, "opening": ""}
        if (latest is not None and snapshot.version == latest["state_version"]
                and not self.store.ready_work(game_id, snapshot.tick)
                and ((record.play_mode or item.mode) != "campaign" or "interaction" in latest)):
            return {**latest, "opening": ""}
        package = package_for_mode(ScenarioPackage.load(item.package_path),
                                         record.play_mode or item.mode)
        result = await self._open_save(item, package, game_id,
                                       opening=package.opening if latest is None else "",
                                       play_mode=record.play_mode or item.mode)
        if latest is None:
            self.accounts.store_intro(game_id, result)
        return result

    async def _open_save(self, item: GameListing, package: ScenarioPackage,
                         game_id: str, opening: str = "",
                         play_mode: str | None = None) -> dict:
        mode = play_mode or item.mode
        if package.story_blueprint is not None:
            clock = (StoryClock(package.ticks_per_day * item.turns_per_story_tick)
                     if package.ticks_per_day is not None else None)
            return turn_view(game_id, item.game_id, mode, "", self.store.load(game_id),
                              GuidanceResult((), "source_story"), opening=opening,
                              presentation_mode=package.presentation_mode, clock=clock,
                              status_fields=package.status_fields, time_unit=package.time_unit)
        if mode == "campaign":
            program, session = self.factory.campaign(item, package, game_id)
            authored = package.authored_prologue if package.presentation_mode == "novel" else ""
            outcome = await session.start(game_id, present_opening=False)
            opening_text = (authored or "\n\n".join(part for part in (opening, outcome.text)
                                                   if part.strip())
                            if package.presentation_mode == "novel" else opening)
            body = "" if package.presentation_mode == "novel" else outcome.text
            segments = (tuple(part for part in outcome.segments if part.kind == "prompt")
                        if package.presentation_mode == "novel" else outcome.segments)
            guidance = await GuidanceAdvisor(self.store, package, program,
                                             telemetry=self.telemetry,
                                             turns_per_story_tick=item.turns_per_story_tick).advise(
                game_id, outcome.snapshot, gate_id=outcome.gate_id,
                complete=outcome.complete, visible_text=opening_text or body)
            action_options: tuple[ActionOption, ...] = ()
            return turn_view(game_id, item.game_id, mode, body,
                              outcome.snapshot, guidance, complete=outcome.complete,
                              opening=opening_text, segments=segments,
                              program=program, gate_id=outcome.gate_id,
                              time_of_day=outcome.time_of_day,
                              presentation_mode=package.presentation_mode,
                              action_options=action_options,
                              status_fields=package.status_fields, time_unit=package.time_unit)
        snapshot = self.store.load(game_id)
        opening_text = (package.authored_prologue or opening
                        if item.mode == "freeform" else opening)
        guidance = await GuidanceAdvisor(self.store, package, telemetry=self.telemetry).advise(
            game_id, snapshot, visible_text=opening_text)
        # Creating a save should not wait for an optional model suggestion.
        action_options: tuple[ActionOption, ...] = ()
        clock = (StoryClock(package.ticks_per_day * item.turns_per_story_tick)
                 if package.ticks_per_day is not None else None)
        return turn_view(game_id, item.game_id, mode, "", snapshot, guidance,
                          opening=opening_text, presentation_mode=package.presentation_mode,
                          action_options=action_options, clock=clock,
                          status_fields=package.status_fields, time_unit=package.time_unit)
