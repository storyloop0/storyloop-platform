"""Read-only next-action guidance shown outside player-facing story prose."""

from __future__ import annotations

from storyloop_harness.generation import ExecutionRequestLimitExceeded, ExecutionTimeout

from dataclasses import dataclass
from typing import Protocol

from storyloop_harness import GameStore
from storyloop_platform.adapters.telemetry import LangfuseTelemetry
from storyloop_harness.telemetry import Telemetry, session_id_for_game
from storyloop_harness.advanced import Snapshot
from storyloop_platform.runtime.campaign import CampaignProgram
from storyloop_harness import ScenarioPackage


@dataclass(frozen=True)
class GuidanceContext:
    game_id: str
    tick: int
    nearby_actors: tuple[tuple[str, str], ...]
    recent_target_ids: tuple[str, ...]
    unmet_actor_ids: tuple[str, ...]
    due_prompt: str | None
    due_kind: str | None
    skip_available: bool
    campaign_progress: float | None
    complete: bool


@dataclass(frozen=True)
class GuidanceResult:
    items: tuple[str, ...]
    source: str


class GuidanceProvider(Protocol):
    async def suggest(self, context: GuidanceContext) -> tuple[str, ...]: ...


class RuleGuidanceProvider:
    """Offer reachable actions without reading NPC secrets or inventing events."""

    async def suggest(self, context: GuidanceContext) -> tuple[str, ...]:
        if context.complete:
            return ("本局已结束。可以回顾经历，或换一个 game-id 开启新游戏。",)
        if context.due_kind is not None:
            if context.due_kind == "continue":
                return ("读完这一段后，点击继续阅读进入下一幕。",)
            if context.due_kind == "message":
                skip = "；不想留言可输入 /choose skip" if context.skip_available else ""
                return (f"当前需要「{context.due_prompt}」；给已认识的嘉宾留言请用 /choose 选项ID 留言内容{skip}。",)
            return (f"先完成当前选择「{context.due_prompt}」；按上方选项输入 /choose 选项ID。",)

        nearby = dict(context.nearby_actors)
        last_target = next((actor_id for actor_id in reversed(context.recent_target_ids)
                            if actor_id in nearby), None)
        new_actor = next((actor_id for actor_id in context.unmet_actor_ids if actor_id in nearby), None)
        if context.campaign_progress is not None and context.campaign_progress >= 0.8:
            first = "主线已接近尾声，可以回顾尚未问清的线索或关系。"
        elif last_target is not None:
            first = f"可以继续追问{nearby[last_target]}刚才的话，或转向场景中的其他人。"
        elif new_actor is not None:
            first = f"可以先和{nearby[new_actor]}聊聊，了解对方此刻的想法。"
        elif nearby:
            first = f"可以和{next(iter(nearby.values()))}交谈，看看此刻有什么新变化。"
        else:
            first = "可以先观察周围，寻找当前能接触的人或物。"

        if context.campaign_progress is not None:
            return (first, "可以用 /next 推进一个时段；到了晚上，也可以用 /rest 休息到次日。")
        return (first, "也可以描述你想观察的地方，或明确说出下一步行动。")


class GuidanceAdvisor:
    """Build a player-safe context and isolate optional provider failures."""

    def __init__(self, store: GameStore, package: ScenarioPackage,
                 program: CampaignProgram | None = None,
                 provider: GuidanceProvider | None = None,
                 telemetry: Telemetry | None = None,
                 turns_per_story_tick: int = 1) -> None:
        if type(turns_per_story_tick) is not int or turns_per_story_tick < 1:
            raise ValueError("turns_per_story_tick must be positive")
        self.store = store
        self.package = package
        self.program = program
        self.provider = provider or RuleGuidanceProvider()
        self.fallback = RuleGuidanceProvider()
        self.telemetry = telemetry or LangfuseTelemetry()
        self.turns_per_story_tick = turns_per_story_tick

    async def advise(self, game_id: str, snapshot: Snapshot, *, gate_id: str | None = None,
                     complete: bool = False, visible_text: str = "") -> GuidanceResult:
        if snapshot.game_id != game_id:
            raise ValueError("guidance snapshot belongs to another game")
        context = self._context(game_id, snapshot, gate_id, complete, visible_text)
        with self.telemetry.span(
            "player-guidance",
            {"game_id": game_id, "tick": snapshot.tick, "gate_id": gate_id,
             "campaign_progress": context.campaign_progress},
            session_id=session_id_for_game(game_id, self.package.package_id),
        ) as span:
            source = "provider"
            try:
                items = await self.provider.suggest(context)
                if (not isinstance(items, (tuple, list)) or not items
                        or any(not isinstance(item, str) or not item.strip() for item in items)):
                    raise ValueError("guidance provider returned no usable suggestions")
                items = tuple(item.strip() for item in items[:2])
            except (ExecutionRequestLimitExceeded, ExecutionTimeout):
                raise
            except Exception:
                items = await self.fallback.suggest(context)
                source = "fallback"
            result = GuidanceResult(tuple(items), source)
            span.update(metadata={"source": source, "item_count": len(result.items)})
            span.metric("story.guidance_items", float(len(result.items)))
            if self.telemetry.capture_content:
                span.update(output=result.items)
            return result

    def _context(self, game_id: str, snapshot: Snapshot, gate_id: str | None,
                 complete: bool, visible_text: str) -> GuidanceContext:
        actors = snapshot.data.get("actors")
        actor_states = actors if isinstance(actors, dict) else {}
        player = actor_states.get("player")
        location = player.get("location") if isinstance(player, dict) else None
        # Authoritative locations alone can reveal people the player has not seen.
        # Speaker labels are emitted only for player-visible NPC replies.
        nearby = tuple((actor_id, self.package.actor_names.get(actor_id, actor_id))
                       for actor_id, _ in self.package.actor_cards
                       if location is not None
                       and f"【{self.package.actor_names.get(actor_id, actor_id)}】" in visible_text
                       if isinstance(actor_states.get(actor_id), dict)
                       and actor_states[actor_id].get("location") == location)
        history = self.store.player_inputs_for(game_id)[-3:]
        recent_targets = tuple(actor_id for entry in history for actor_id in entry.target_ids)
        campaign = snapshot.data.get("campaign")
        met = campaign.get("met") if isinstance(campaign, dict) else None
        unmet = tuple(actor_id for actor_id, _ in nearby
                      if isinstance(met, dict) and met.get(actor_id) is False)
        due_step = None
        if gate_id is not None and self.program is not None:
            due_step = next((step for step in self.program.steps
                             if step["id"] == gate_id and step["kind"] in {"continue", "choice", "message"}
                             and step["at"] <= snapshot.tick // self.turns_per_story_tick), None)
        progress = (min(1.0, (snapshot.tick // self.turns_per_story_tick) / self.program.final_tick)
                    if self.program is not None else None)
        return GuidanceContext(game_id, snapshot.tick, nearby, recent_targets, unmet,
                               due_step["prompt"] if due_step else None,
                               due_step["kind"] if due_step else None,
                               any(option["id"] == "skip" for option in due_step.get("options", []))
                               if due_step else False,
                               progress, complete)
