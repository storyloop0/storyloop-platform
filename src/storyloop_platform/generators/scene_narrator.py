"""Player-facing atmosphere around committed campaign events."""

from __future__ import annotations

import json

from storyloop_harness.generation import Msg, ExecutionRequestLimitExceeded, ExecutionTimeout
from agentscope.model import ChatModelBase
from pydantic import BaseModel, Field

from storyloop_platform.adapters.telemetry import LangfuseTelemetry
from storyloop_harness.telemetry import Telemetry
from storyloop_harness.generation import ThinkingSafeOpenAIChatFormatter
from storyloop_platform.runtime.campaign import CampaignProgram
from storyloop_harness.advanced import SceneContext
from storyloop_harness import ScenarioPackage
from storyloop_harness.advanced import current_player_preferences


class SceneNarration(BaseModel):
    text: str = Field(min_length=1)


class CampaignSceneNarrator:
    """Narrate a visible scene without changing the game state or speaking for NPCs."""

    def __init__(self, package: ScenarioPackage, program: CampaignProgram,
                 model: ChatModelBase, telemetry: Telemetry | None = None) -> None:
        self.package = package
        self.program = program
        self.model = model
        self.telemetry = telemetry or LangfuseTelemetry()
        self.formatter = ThinkingSafeOpenAIChatFormatter()

    def _choice_labels(self, context: SceneContext) -> list[str]:
        choices = context.snapshot.data.get("campaign", {}).get("choices", {})
        labels: list[str] = []
        for step in self.program.steps:
            if step["kind"] not in {"choice", "message"}:
                continue
            selected = choices.get(step["id"])
            option = next((item for item in step["options"] if item["id"] == selected), None)
            if option is not None:
                labels.append(option["label"][:80])
        return labels[-3:]

    def _known_nearby(self, context: SceneContext) -> list[str]:
        state = context.snapshot.data
        actors = state.get("actors", {})
        campaign = state.get("campaign", {})
        if not isinstance(actors, dict) or not isinstance(campaign, dict):
            return []
        player = actors.get("player", {})
        location = player.get("location") if isinstance(player, dict) else None
        met = campaign.get("met", {})
        if not isinstance(met, dict):
            return []
        return [self.package.actor_names[actor_id]
                for actor_id, actor in actors.items()
                if actor_id in self.package.actor_names and met.get(actor_id)
                and isinstance(actor, dict) and actor.get("location") == location][:8]

    def _request(self, context: SceneContext) -> dict[str, object]:
        lore = self.package.worldbook.visible_lore("player", limit=4)
        visible = [{"kind": part.kind, "speaker": part.speaker_name, "text": part.text[:900]}
                   for part in context.segments[-8:] if part.kind != "prompt"]
        opening_scene = [step["text"][:900] for step in self.program.steps
                         if step["kind"] == "scene" and step["at"] == 0][:2]
        return {
            "phase": "opening_after_choices" if context.opening else "turn_continuation",
            "day": context.day,
            "period": context.period,
            "public_setting": [entry.text[:900] for entry in lore],
            "scenario_opening": self.package.opening[:900] if context.opening else "",
            "initial_scene": opening_scene if context.opening else [],
            "selected_choices": self._choice_labels(context) if context.opening else [],
            "known_nearby_people": self._known_nearby(context),
            "player_action": context.player_text[:400] if not context.player_text.startswith("/") else "",
            "player_preferences": list(current_player_preferences()),
            "committed_visible_results": visible,
        }

    @staticmethod
    def _fallback(context: SceneContext, request: dict[str, object]) -> str:
        lore = request["public_setting"]
        setting = str(lore[0]).split("。", 1)[0] if isinstance(lore, list) and lore else "周围的场景"
        setting = setting[:90].rstrip("，；")
        if context.opening:
            return (f"{setting}。开场的选择已经记下，故事从这里开始。"
                    "你可以先看看周围，与在场的人打招呼，或直接说出你想做的事。")
        return (f"第{context.day}天的{context.period}，{setting}。"
                "刚才的互动暂时告一段落，你可以继续观察或与在场的人交谈。")

    async def present(self, context: SceneContext) -> str:
        request = self._request(context)
        system = (
            "你是互动故事的场景叙述者。只写一段约80至150字、给玩家看的中文叙述。"
            "根据提供的公开设定和本轮已发生的可见结果，描绘此刻的空间、氛围与时间流动，"
            "让上一段角色回应自然落在场景中。不要复述角色台词，不要替角色说新台词，"
            "不要替玩家作选择或执行额外行动，不要制造新的事件、物品、天气变化或秘密。"
            "没有依据的感官细节只可作低风险的文学描写，不得写成确定的世界事实。"
            "玩家画像只用于微调文风和节奏，不是剧情事实，也不能覆盖本轮玩家输入。"
            "若 phase 为 opening_after_choices，需要交代故事的地点与情境，说明玩家现在"
            "可以观察、找在场的人交谈或自由行动；此时不要直接跳到下一时段。"
            "若 phase 为 turn_continuation，在角色回复之后补一段环境与节奏上的承接，"
            "不要写成系统提示或选项列表。仅输出符合 SceneNarration 的结构化结果。"
        )
        try:
            prompt = await self.formatter.format(msgs=[
                Msg("system", system, "system"),
                Msg("player", json.dumps(request, ensure_ascii=False), "user"),
            ])
            with self.telemetry.span(
                "scene-narration-model",
                {"opening": context.opening, "public_lore_count": len(request["public_setting"]),
                 "visible_result_count": len(request["committed_visible_results"])},
                kind="agent", input=request if self.telemetry.capture_content else None,
            ) as span:
                response = await self.model(prompt, structured_model=SceneNarration)
                narration = SceneNarration.model_validate(response.metadata).text.strip()
                if not narration:
                    raise ValueError("scene narration is empty")
                if self.telemetry.capture_content:
                    span.update(output=narration)
                return narration
        except (ExecutionRequestLimitExceeded, ExecutionTimeout):
            raise
        except Exception as error:
            with self.telemetry.span("scene-narration-fallback",
                                     {"error_type": type(error).__name__}) as span:
                span.metric("story.scene_narration_fallback", 1.0)
                return self._fallback(context, request)
