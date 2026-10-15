"""Second-person player-visible prose from already committed story beats."""

from __future__ import annotations

import json
from collections.abc import Callable

from storyloop_harness.generation import Msg, ExecutionRequestLimitExceeded, ExecutionTimeout
from agentscope.model import ChatModelBase
from pydantic import BaseModel, Field

from storyloop_platform.adapters.telemetry import LangfuseTelemetry
from storyloop_harness.telemetry import Telemetry, session_id_for_game
from storyloop_harness.generation import ThinkingSafeOpenAIChatFormatter
from storyloop_harness.advanced import SceneContext
from storyloop_harness.advanced import current_player_preferences
from storyloop_harness import ScenarioPackage


class NovelProse(BaseModel):
    text: str = Field(min_length=1)


_IMAGE_FAMILIES = {
    "光线与明暗": ("光", "亮", "照进", "照在"),
    "窗与玻璃": ("窗", "玻璃"),
    "雪景": ("雪",),
    "室内暖意": ("暖", "热气"),
    "地板": ("地板",),
}


def repeated_imagery(passages: list[str]) -> list[str]:
    """Flag imagery repeated across recent turns without another model call."""
    if len(passages) < 2:
        return []
    return [name for name, words in _IMAGE_FAMILIES.items()
            if sum(any(word in passage for word in words) for passage in passages) >= 2][:3]


class NovelTurnNarrator:
    """Compose one story passage; state changes remain exclusively with the runtime."""

    def __init__(self, package: ScenarioPackage, model: ChatModelBase,
                 telemetry: Telemetry | None = None,
                 recent_prose: Callable[[str], list[str]] | None = None) -> None:
        self.package = package
        self.model = model
        self.telemetry = telemetry or LangfuseTelemetry()
        self.formatter = ThinkingSafeOpenAIChatFormatter()
        self.recent_prose = recent_prose

    async def present(self, context: SceneContext) -> str:
        recent = ([part[-650:] for part in self.recent_prose(context.game_id)[-3:]]
                  if self.recent_prose is not None else [])
        beats = [
            {"kind": part.kind, "speaker": part.speaker_name, "text": part.text[:1600]}
            for part in context.segments if part.kind != "prompt" and part.text.strip()
        ]
        dialogue_only = bool(beats) and all(
            beat["kind"] in {"dialogue", "time"} for beat in beats
        )
        source_length = sum(len(beat["text"]) for beat in beats)
        target_length = (
            "80至140字" if dialogue_only else
            "300至480字" if source_length >= 520 else
            "200至340字" if source_length >= 260 else
            "80至160字" if len(beats) <= 1 else "150至300字"
        )
        request = {
            "player_action": (context.player_text[:1000]
                              if not context.player_text.startswith("/") else ""),
            "day": context.day,
            "period": context.period,
            "opening": context.opening,
            "current_goal": context.current_goal[:300],
            "public_setting": [item.text[:900] for item in
                               self.package.worldbook.visible_lore("player", limit=4)],
            "public_rules": [item.text[:900] for item in
                             self.package.worldbook.visible_rules("player", limit=4)],
            "player_profile": self.package.initial_state.get("player_profile", {}),
            "scenario_opening": self.package.opening[:900] if context.opening else "",
            "visible_beats": beats[-16:],
            "dialogue_only": dialogue_only,
            "target_length": target_length,
            "recent_player_visible_prose": recent,
            "avoid_repeated_imagery": repeated_imagery(recent),
            "player_style_preferences": list(current_player_preferences()),
        }
        system = (
            "你是文游主控的小说模式呈现器。把本轮已经发生的玩家可见结果整合为一段连贯的中文小说正文，"
            "以第二人称“你”叙述玩家的行动和所见；无论近期正文使用什么人称，你都统一写“你”。"
            "严格根据 target_length 控制篇幅；信息少就写短，绝不用虚构动作或风景凑字数。"
            "按可见结果的发生顺序自然串起行动、环境、"
            "时间流逝和NPC回应；可依据公开设定描绘环境氛围，NPC的话可以引用，但不能新增台词或改写其事实含义。"
            "多人初见时保留有辨识度的可见外貌、礼貌寒暄与停顿，不要把每个人压成姓名清单；"
            "遵守 public_rules 的公开时点，角色尚未公开的年龄、职业等信息不得在正文里提前揭示；"
            "player_profile 只用于正确叙述主角已知身份，不能扩写成未发生的行动；"
            "用双方可观察的语气和动作呈现初见的局促或好奇，不替玩家断言心动与否。"
            "如果 visible_beats 中有 time，利用光线、环境与已经发生的事侧写时段变化；"
            "傍晚或夜深时可让叙述自然显出一天将结束，但不能替玩家决定睡觉或跳天。"
            "不要把第几天、几点或耗时提示写成独立的系统播报；这些数值由界面进度区记录。"
            "current_goal 只用于从已出现的人和事中轻轻引出下一步，不写成任务清单，"
            "也不提前透露尚未发生的节目安排。"
            "玩家行动只以 player_action 为准，不替玩家增加行为、对白、决定、感情或内心独白。"
            "recent_player_visible_prose 只用于衔接刚发生的事和避开重复表达，不要复述它。"
            "本轮不要描写 avoid_repeated_imagery 中的景物，也不要换同义词继续描写它们；"
            "可以直接从玩家的问话或角色的回答入手。"
            "每轮优先抓住本轮独有的动作、物件、语气或关系变化；环境描写只选与此刻行动有关的一两处。"
            "若近几轮反复出现窗外、雪光、上午的光、木地板等意象，本轮换用人物动作、声音或物件承接，"
            "不要套用固定的天气或光线开头。"
            "玩家发送私人留言时，准确呈现收件人和原文；除非 visible_beats 明确给出了回信，"
            "不能编造对方已读、即时回应或玩家的心理结论。"
            "若 visible_beats 只给出角色台词，不得新增角色看人、笑、转身、碰物件等动作；"
            "可用句式和停顿组织文字，不把未经提交的动作写成事实。"
            "dialogue_only 为 true 时，正文围绕玩家的问题、角色回答中的态度与保留展开，"
            "尽量只用一段或两段；"
            "不要另起环境描写或添加没有出现在 visible_beats 中的道具和动作。"
            "不得透露未提供的秘密，不得制造新事件、人物、物品或关系；可用低风险的文学连接词。"
            "玩家画像只微调文风和节奏，不是剧情事实。不要系统提示或建议列表；"
            "不要用第一人称“我”代替玩家叙述，NPC原有的直接引语可以保留其人称。"
            "没有可见事件时简短描绘停顿，不要凭空推进情节。仅输出 NovelProse 结构化结果。"
        )
        try:
            prompt = await self.formatter.format(msgs=[
                Msg("system", system, "system"),
                Msg("player", json.dumps(request, ensure_ascii=False), "user"),
            ])
            with self.telemetry.span(
                "novel-presentation-model",
                {"beat_count": len(beats), "opening": context.opening},
                kind="agent", input=request if self.telemetry.capture_content else None,
                session_id=session_id_for_game(context.game_id, self.package.package_id),
            ) as span:
                response = await self.model(prompt, structured_model=NovelProse)
                prose = NovelProse.model_validate(response.metadata).text.strip()
                prose = prose.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\r", "\n")
                prose = "\n".join(
                    line[:-1] if line.endswith('"') and line.count('"') % 2 else line
                    for line in prose.splitlines()
                ).strip()
                if not prose:
                    raise ValueError("novel presenter returned empty prose")
                if self.telemetry.capture_content:
                    span.update(output=prose)
                return prose
        except (ExecutionRequestLimitExceeded, ExecutionTimeout):
            raise
        except Exception as error:
            with self.telemetry.span("novel-presentation-fallback",
                                     {"error_type": type(error).__name__},
                                     session_id=session_id_for_game(
                                         context.game_id, self.package.package_id)) as span:
                span.metric("story.novel_presentation_fallback", 1.0)
            if not beats:
                return "你停下来观察眼前的情形，等着下一步行动。"
            parts = []
            for beat in beats:
                speaker = beat["speaker"]
                if speaker:
                    parts.append(f"你听见{speaker}的回应：{beat['text']}")
                else:
                    parts.append(f"你留意到：{beat['text']}")
            return "\n\n".join(parts)
