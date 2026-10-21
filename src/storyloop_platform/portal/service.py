"""Authenticated application facade."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

from storyloop_harness.advanced import TurnProgress

from storyloop_platform.config import PlatformSettings, PlatformResources
from storyloop_platform.gameplay.service import GameplayService
from storyloop_platform.gameplay.fact_queries import FactQueryService
from storyloop_platform.gameplay.turns import TurnExecutionService
from storyloop_platform.lifecycle import OperationSupervisor, GRACE_TIMEOUT, CANCEL_TIMEOUT
from storyloop_platform.memory.service import PlayerMemoryService
from storyloop_platform.portal.access import AccessService
from storyloop_platform.portal.invitations import InvitationService
from storyloop_platform.portal.moderation import ScenarioModerationService
from storyloop_platform.portal.sql_billing import SQLBillingRepository
from storyloop_platform.portal.sql_repository import SQLPlayerRepository
from storyloop_platform.portal.user_scenarios import UserScenarioService


class PlayerPortal:
    """Authenticated application facade; all collaborators are composed by bootstrap."""

    def __init__(self, *, settings: PlatformSettings, resources: PlatformResources,
                 db_path: str | None, accounts: SQLPlayerRepository, access: AccessService,
                 invitations: InvitationService, user_scenarios: UserScenarioService,
                 moderation: ScenarioModerationService, billing: SQLBillingRepository | None,
                 memory_service: PlayerMemoryService, gameplay: GameplayService,
                 turns: TurnExecutionService, fact_queries: FactQueryService,
                 operations: OperationSupervisor,
                 close_resources: Callable[[], None],
                 shutdown_resources: Callable[[float, float], Awaitable[None]]) -> None:
        self.settings = settings
        self.resources = resources
        self.db_path = db_path
        self.accounts = accounts
        self.access = access
        self.invitations = invitations
        self.user_scenarios = user_scenarios
        self.moderation = moderation
        self.billing = billing
        self.memory_service = memory_service
        self.gameplay = gameplay
        self.turns = turns
        self.fact_queries = fact_queries
        self.operations = operations
        self._close_resources = close_resources
        self._shutdown_resources = shutdown_resources

    def close(self) -> None:
        self._close_resources()

    async def shutdown(self, *, grace_timeout: float = GRACE_TIMEOUT,
                       cancel_timeout: float = CANCEL_TIMEOUT) -> None:
        await self._shutdown_resources(grace_timeout, cancel_timeout)

    async def memory_status(self, token: str) -> dict:
        return await self.memory_service.status(self.accounts.resolve_token(token))

    async def set_memory_enabled(self, token: str, enabled: bool) -> dict:
        return await self.memory_service.set_enabled(self.accounts.resolve_token(token), enabled)

    async def clear_memory(self, token: str) -> dict:
        return await self.memory_service.clear(self.accounts.resolve_token(token))

    async def correct_memory(self, token: str, memory_id: str, text: str) -> dict:
        return await self.memory_service.correct(self.accounts.resolve_token(token), memory_id, text)

    async def delete_memory(self, token: str, memory_id: str) -> dict:
        return await self.memory_service.delete_one(self.accounts.resolve_token(token), memory_id)

    def register(self, username: str, password: str, invite_code: str | None = None) -> dict:
        player_id = self.accounts.register(username, password, invite_code,
                                           require_invite=self.settings.environment == "online")
        if self.billing is not None:
            self.billing.ensure_wallet(player_id)
        return {"player_id": player_id, "token": self.accounts.issue_token(player_id)}

    def login(self, username: str, password: str) -> dict:
        player_id = self.accounts.authenticate(username, password)
        if self.billing is not None:
            self.billing.ensure_wallet(player_id)
        return {"player_id": player_id, "token": self.accounts.issue_token(player_id)}

    def wallet(self, token: str) -> dict:
        player_id = self.accounts.resolve_token(token)
        if self.billing is None:
            raise ValueError("billing is not configured")
        return self.billing.wallet(player_id)

    def credit_ledger(self, token: str, limit: int = 30) -> list[dict]:
        player_id = self.accounts.resolve_token(token)
        if self.billing is None:
            raise ValueError("billing is not configured")
        return self.billing.ledger(player_id, limit)

    def logout(self, token: str) -> None:
        self.accounts.resolve_token(token)
        self.accounts.revoke_token(token)

    def session_info(self, token: str) -> dict:
        player_id = self.accounts.resolve_token(token)
        return {"player_id": player_id, "roles": list(self.access.roles(player_id)),
                "capabilities": list(self.access.capabilities(player_id))}

    async def upload_scenario(self, token: str, title: str, summary: str, archive: bytes) -> dict:
        player_id = self.accounts.resolve_token(token)
        return await self.user_scenarios.upload(player_id, title, summary, archive)

    async def upload_scenario_version(self, token: str, scenario_id: str, title: str,
                                      summary: str, archive: bytes) -> dict:
        player_id = self.accounts.resolve_token(token)
        return await self.user_scenarios.upload_version(player_id, scenario_id, title, summary, archive)

    def submit_scenario(self, token: str, scenario_id: str) -> dict:
        return self.moderation.submit(self.accounts.resolve_token(token), scenario_id)

    def my_submissions(self, token: str) -> list[dict]:
        return self.moderation.list_mine(self.accounts.resolve_token(token))

    def withdraw_submission(self, token: str, submission_id: str) -> dict:
        return self.moderation.withdraw(self.accounts.resolve_token(token), submission_id)

    def review_queue(self, token: str, status: str = "pending") -> list[dict]:
        return self.moderation.queue(self.accounts.resolve_token(token), status)

    def review_detail(self, token: str, submission_id: str) -> dict:
        return self.moderation.detail(self.accounts.resolve_token(token), submission_id)

    def review_decide(self, token: str, submission_id: str, decision: str,
                      reason: str = "") -> dict:
        return self.moderation.decide(self.accounts.resolve_token(token), submission_id,
                                      decision, reason)

    def admin_users(self, token: str) -> list[dict]:
        return self.access.list_users(self.accounts.resolve_token(token))

    def admin_issue_invitations(self, token: str, count: int = 1) -> dict:
        return self.invitations.issue(self.accounts.resolve_token(token), count)

    def admin_invitations(self, token: str) -> list[dict]:
        return self.invitations.list_invitations(self.accounts.resolve_token(token))

    def admin_revoke_invitation(self, token: str, invitation_id: str) -> dict:
        return self.invitations.revoke(self.accounts.resolve_token(token), invitation_id)

    def admin_set_role(self, token: str, player_id: str, role: str, enabled: bool) -> dict:
        admin_id = self.accounts.resolve_token(token)
        self.access.set_role(admin_id, player_id, role, enabled)
        return {"player_id": player_id, "roles": list(self.access.roles(player_id))}

    def admin_set_status(self, token: str, player_id: str, status: str) -> dict:
        self.access.set_status(self.accounts.resolve_token(token), player_id, status)
        return {"player_id": player_id, "status": status}

    def admin_releases(self, token: str) -> list[dict]:
        return self.moderation.releases(self.accounts.resolve_token(token))

    def admin_release_state(self, token: str, scenario_id: str,
                            state: str, reason: str) -> dict:
        return self.moderation.set_release_state(self.accounts.resolve_token(token),
                                                 scenario_id, state, reason)

    def admin_audit(self, token: str) -> list[dict]:
        return self.access.list_audit(self.accounts.resolve_token(token))

    def my_scenarios(self, token: str) -> list[dict]:
        player_id = self.accounts.resolve_token(token)
        return self.user_scenarios.list_mine(player_id)

    def publish_scenario(self, token: str, scenario_id: str) -> dict:
        player_id = self.accounts.resolve_token(token)
        return self.user_scenarios.publish(player_id, scenario_id)

    def delete_scenario_draft(self, token: str, scenario_id: str) -> dict:
        player_id = self.accounts.resolve_token(token)
        return self.user_scenarios.delete_draft(player_id, scenario_id)

    def games(self, token: str) -> list[dict]:
        return self.gameplay.games(self.accounts.resolve_token(token))

    def cover_artwork(self, token: str, catalog_id: str) -> Path:
        return self.gameplay.cover_artwork(self.accounts.resolve_token(token), catalog_id)

    def player_card(self, token: str, game_id: str) -> dict:
        return self.gameplay.player_card(self.accounts.resolve_token(token), game_id)

    def cast(self, token: str, game_id: str) -> list[dict]:
        return self.gameplay.cast(self.accounts.resolve_token(token), game_id)

    def character_detail(self, token: str, game_id: str, actor_id: str) -> dict:
        return self.gameplay.character_detail(self.accounts.resolve_token(token), game_id, actor_id)

    def portrait_artwork(self, token: str, game_id: str, actor_id: str) -> Path:
        return self.gameplay.portrait_artwork(self.accounts.resolve_token(token), game_id, actor_id)

    def history(self, token: str, game_id: str) -> dict:
        return self.gameplay.history(self.accounts.resolve_token(token), game_id)

    def saves(self, token: str) -> list[dict]:
        return self.gameplay.saves(self.accounts.resolve_token(token))

    def get_save_settings(self, token: str, game_id: str) -> dict:
        return self.gameplay.get_save_settings(self.accounts.resolve_token(token), game_id)

    async def set_save_settings(self, token: str, game_id: str,
                                temperature: float, context_window_tokens: int) -> dict:
        return await self.gameplay.set_save_settings(self.accounts.resolve_token(token), game_id, temperature, context_window_tokens)

    async def create_save(self, token: str, catalog_id: str,
                          play_mode: str | None = None,
                          story_setup: dict[str, str] | None = None) -> dict:
        return await self.gameplay.create_save(self.accounts.resolve_token(token), catalog_id, play_mode, story_setup)

    async def resume_save(self, token: str, game_id: str) -> dict:
        return await self.gameplay.resume_save(self.accounts.resolve_token(token), game_id)

    async def create_review_preview(self, token: str, submission_id: str) -> dict:
        return await self.gameplay.create_review_preview(self.accounts.resolve_token(token), submission_id)

    async def turn(self, token: str, game_id: str, text: str,
                   request_id: str | None = None, progress: TurnProgress | None = None) -> dict:
        return await self.turns.turn(self.accounts.resolve_token(token), game_id, text, request_id, progress)

    async def fact_query(self, token: str, game_id: str, request_id: str,
                         fact_id: str, speaker_id: str) -> dict:
        return await self.fact_queries.query(self.accounts.resolve_token(token),
                                             game_id, request_id, fact_id, speaker_id)

    def turn_recovery_status(self, token: str, game_id: str,
                             request_id: str, input_hash: str) -> dict:
        return self.turns.recovery_status(self.accounts.resolve_token(token), game_id,
                                          request_id, input_hash)
