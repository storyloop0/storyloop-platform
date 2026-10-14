"""Read-only qualification of an ordinary player's durable turn evidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import text
from storyloop_harness import ScenarioPackage
from storyloop_harness.advanced import snapshot_state_fingerprint

from storyloop_platform.api.contracts import View
from storyloop_platform.gameplay.scene_recovery import SceneRecoveryPlan, SceneRecoveryRuntime
from storyloop_platform.portal.turn_recovery_contexts import TurnRecoveryContext


Reason = Literal["RECEIPT_READY", "PREPARED_READY", "SCENE_READY", "INPUT_MISMATCH",
                 "LATER_REQUEST", "EVIDENCE_INVALID", "CONFIG_CHANGED", "USAGE_UNAVAILABLE",
                 "LEGACY_REQUEST", "NO_SNAPSHOT", "UNSUPPORTED"]


@dataclass(frozen=True)
class RecoveryQualification:
    reason: Reason
    context: TurnRecoveryContext | None = None
    plan: SceneRecoveryPlan | None = None
    inspection: object | None = None

    def status(self, request_id: str) -> dict:
        status = ("complete" if self.reason == "RECEIPT_READY" else
                  "recoverable" if self.reason in ("PREPARED_READY", "SCENE_READY") else
                  "not_available" if self.reason in ("NO_SNAPSHOT", "UNSUPPORTED") else "blocked")
        return {"request_id": request_id, "status": status,
                "can_recover": status in ("complete", "recoverable"),
                "reason_code": self.reason}


def validate_scene_usage(inspection, context: TurnRecoveryContext) -> list:
    """Only covered, priced successful generation records can fund a paid receipt."""
    snapshot = inspection.snapshot
    usage = list(snapshot.success_usage())
    if not context.billing_enabled:
        return usage
    if snapshot.metadata()["usage_coverage"] != "reported" or not usage:
        raise ValueError("scene usage unavailable")
    policy = context.policy()
    if policy is None:
        raise ValueError("scene policy unavailable")
    policy.price_milli_points(usage)
    return usage


def inspect_scene(*, store, factory, contexts, game_id: str, request_id: str,
                  input_hash: str, item, package: ScenarioPackage,
                  context: TurnRecoveryContext) -> RecoveryQualification:
    if context.input_hash != input_hash:
        return RecoveryQualification("INPUT_MISMATCH")
    if not contexts.is_latest(game_id, request_id):
        return RecoveryQualification("LATER_REQUEST")
    history = store.player_inputs_for(game_id)
    if history and not history[-1].event_id.startswith(context.turn_id + ":"):
        with contexts.engine.connect() as db:
            committed_version = db.execute(text("""SELECT state_version FROM events
                WHERE game_id=:game_id AND event_id=:event_id"""),
                {"game_id": game_id, "event_id": history[-1].event_id}).scalar_one_or_none()
        if committed_version is None:
            return RecoveryQualification("EVIDENCE_INVALID")
        if committed_version > context.base_version:
            return RecoveryQualification("LATER_REQUEST")
    if not isinstance(factory, SceneRecoveryRuntime):
        return RecoveryQualification("CONFIG_CHANGED")
    try:
        plan = factory.scene_recovery_plan(item, package, game_id)
    except ValueError:
        return RecoveryQualification("CONFIG_CHANGED")
    if plan is None or plan.generation_identity != context.generation_identity:
        return RecoveryQualification("CONFIG_CHANGED")
    engine = factory.scene_recovery_engine(item, package, game_id, plan=plan, recovery_only=True)
    inspection = engine.inspect_generated_scene_recovery_fingerprint(
        game_id, context.turn_id, input_hash, package.version)
    if inspection is None:
        current = store.load(game_id)
        if (current.version != context.base_version or current.tick != context.base_tick or
                snapshot_state_fingerprint(current) != context.base_state_hash):
            return RecoveryQualification("EVIDENCE_INVALID")
        return RecoveryQualification("NO_SNAPSHOT", context=context, plan=plan)
    metadata = inspection.snapshot.metadata()
    if (metadata["base_version"] != context.base_version or
            metadata["base_tick"] != context.base_tick or
            metadata["base_state_hash"] != context.base_state_hash):
        return RecoveryQualification("EVIDENCE_INVALID")
    try:
        validate_scene_usage(inspection, context)
    except ValueError:
        return RecoveryQualification("USAGE_UNAVAILABLE")
    return RecoveryQualification("SCENE_READY", context, plan, inspection)


def valid_cached_view(value: dict) -> bool:
    try:
        View.model_validate(value)
    except (ValueError, TypeError):
        return False
    return True
