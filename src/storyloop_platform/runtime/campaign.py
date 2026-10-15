"""Data-driven campaign clock and player choice gates around a scene turn engine."""

from __future__ import annotations

from storyloop_harness import TurnInput
from storyloop_harness.generation import ExecutionRequestLimitExceeded, ExecutionTimeout
from storyloop_harness.usage import ModelUsage

import asyncio
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol
from weakref import WeakValueDictionary

from storyloop_harness import GameStore
from storyloop_platform.adapters.telemetry import LangfuseTelemetry
from storyloop_harness.telemetry import Telemetry, session_id_for_game
from storyloop_harness.advanced import Effect, Observation, Snapshot, WorldEvent
from storyloop_harness.advanced import SceneContext, StorySegment, segment_for_observation
from storyloop_harness.advanced import TurnProgress, emit
from storyloop_harness.advanced import StoryClock


DisplayPart = str | StorySegment


class SceneTurnEngine(Protocol):
    package: Any
    async def run_turn(self, turn: TurnInput, *, max_tick: int | None = None,
                       progress: TurnProgress | None = None) -> Any: ...


class ScenePresenter(Protocol):
    async def present(self, context: SceneContext) -> str: ...


class MessageBatchWriter(Protocol):
    async def write(self, game_id: str, senders: tuple[str, ...],
                    recipient: str | None, player_note: str,
                    fallback: dict[str, str]) -> Any: ...


@dataclass(frozen=True)
class CampaignOutcome:
    text: str
    snapshot: Snapshot
    gate_id: str | None
    complete: bool
    segments: tuple[StorySegment, ...] = ()
    time_of_day: str = ""
    model_usage: tuple[ModelUsage, ...] = ()


@dataclass(frozen=True)
class CampaignProgram:
    program_id: str
    ticks_per_day: int
    final_tick: int
    steps: tuple[dict[str, Any], ...]

    @classmethod
    def load(cls, path: str | Path) -> CampaignProgram:
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> CampaignProgram:
        if not isinstance(raw, dict):
            raise ValueError("campaign must be an object")
        program_id, ticks_per_day, final_tick = raw.get("id"), raw.get("ticks_per_day"), raw.get("final_tick")
        steps = raw.get("steps")
        if not isinstance(program_id, str) or not program_id:
            raise ValueError("campaign requires an ID")
        if type(ticks_per_day) is not int or ticks_per_day < 1:
            raise ValueError("campaign requires positive ticks_per_day")
        if type(final_tick) is not int or final_tick < 1:
            raise ValueError("campaign requires positive final_tick")
        if not isinstance(steps, list) or not steps:
            raise ValueError("campaign requires steps")
        seen: set[str] = set()
        previous = (-1, -1)
        for step in steps:
            if not isinstance(step, dict) or not isinstance(step.get("id"), str) or not step["id"]:
                raise ValueError("campaign step requires an ID")
            if step["id"] in seen:
                raise ValueError("duplicate campaign step ID")
            seen.add(step["id"])
            at = step.get("at")
            if type(at) is not int or not 0 <= at <= final_tick:
                raise ValueError("campaign steps must be ordered within final_tick")
            at_subtick = step.get("at_subtick", 0)
            if type(at_subtick) is not int or at_subtick < 0:
                raise ValueError("campaign at_subtick must be a nonnegative integer")
            if (at, at_subtick) < previous:
                raise ValueError("campaign steps must be ordered within final_tick")
            previous = (at, at_subtick)
            kind = step.get("kind")
            if kind not in {"scene", "continue", "choice", "message", "finale"}:
                raise ValueError("unknown campaign step kind")
            if kind == "continue" and (not isinstance(step.get("prompt"), str)
                                       or not step["prompt"].strip()
                                       or not isinstance(step.get("label"), str)
                                       or not step["label"].strip()):
                raise ValueError("continue requires prompt and label")
            if kind in {"choice", "message"}:
                if not isinstance(step.get("prompt"), str) or not isinstance(step.get("options"), list):
                    raise ValueError("choice requires prompt and options")
                options = step["options"]
                if not options or any(not isinstance(option, dict) or not isinstance(option.get("id"), str)
                                      or not isinstance(option.get("label"), str) for option in options):
                    raise ValueError("invalid campaign choice option")
                if len({option["id"] for option in options}) != len(options):
                    raise ValueError("duplicate campaign option ID")
            if kind == "message":
                for item in step.get("incoming", []):
                    if (not isinstance(item, dict) or not isinstance(item.get("actor"), str)
                            or not isinstance(item.get("label"), str)
                            or not isinstance(item.get("text"), str)
                            or type(item.get("min_affinity")) is not int):
                        raise ValueError("invalid incoming message rule")
            if kind == "scene" and not isinstance(step.get("text"), str):
                raise ValueError("scene requires text")
            if kind == "scene":
                goal = step.get("action_goal")
                leads = step.get("action_leads")
                anchors = step.get("action_anchors")
                if goal is not None and (not isinstance(goal, str) or not goal.strip()):
                    raise ValueError("scene action_goal must be nonempty text")
                if anchors is not None and (
                    not isinstance(anchors, list) or not anchors
                    or any(not isinstance(anchor, str) or not anchor.strip() for anchor in anchors)
                ):
                    raise ValueError("scene action_anchors must be nonempty text")
                if leads is not None and (
                    not isinstance(leads, list) or len(leads) != 3
                    or any(not isinstance(lead, dict)
                           or not isinstance(lead.get("label"), str)
                           or not 2 <= len(lead["label"].strip()) <= 50
                           or not isinstance(lead.get("input"), str)
                           or not 2 <= len(lead["input"].strip()) <= 500
                           for lead in leads)
                ):
                    raise ValueError("scene action_leads must contain three labeled actions")
            if kind == "finale" and (not isinstance(step.get("choice_key"), str)
                                      or type(step.get("threshold")) is not int):
                raise ValueError("finale requires choice_key and threshold")
            if kind == "finale" and (not isinstance(step.get("success_text"), str)
                                      or not isinstance(step.get("other_text"), str)):
                raise ValueError("finale requires success_text and other_text")
        finales = [step for step in steps if step["kind"] == "finale"]
        if len(finales) != 1 or steps[-1] is not finales[0] or finales[0]["at"] != final_tick:
            raise ValueError("campaign requires one terminal finale at final_tick")
        gates = {step["id"] for step in steps[:-1] if step["kind"] == "choice"}
        if finales[0]["choice_key"] not in gates:
            raise ValueError("finale must reference an earlier choice")
        return cls(program_id, ticks_per_day, final_tick, tuple(steps))

    def current_action_context(self, snapshot: Snapshot) -> dict[str, Any]:
        """Expose only the latest scene already consumed by this save."""
        campaign = snapshot.data.get("campaign")
        cursor = campaign.get("cursor", 0) if isinstance(campaign, dict) else 0
        if type(cursor) is not int or cursor < 0:
            return {}
        for step in reversed(self.steps[:cursor]):
            if step["kind"] == "scene":
                return {"scene": step["text"][-1200:],
                        "goal": step.get("action_goal", ""),
                        "anchors": step.get("action_anchors", []),
                        "leads": step.get("action_leads", [])}
        return {}

    def initial_state(self, actor_ids: list[str]) -> dict[str, Any]:
        return {
            "cursor": 0,
            "day": 1,
            "choices": {step["id"]: None for step in self.steps if step["kind"] in {"choice", "message"}},
            "answers": {step["id"]: "" for step in self.steps if step["kind"] == "choice"},
            "messages": {step["id"]: None for step in self.steps if step["kind"] == "message"},
            "incoming": {step["id"]: [] for step in self.steps if step["kind"] == "message"},
            "met": {actor_id: False for actor_id in actor_ids},
            "affinity": {actor_id: 0 for actor_id in actor_ids},
            "ending": None,
        }


class CampaignSession:
    """Advance a flexible milestone list without allowing a required choice to be skipped."""

    def __init__(self, store: GameStore, program: CampaignProgram, turn_engine: SceneTurnEngine | None = None,
                 telemetry: Telemetry | None = None, turns_per_story_tick: int = 1,
                 scene_presenter: ScenePresenter | None = None,
                 novel_presenter: ScenePresenter | None = None,
                 message_batch_writer: MessageBatchWriter | None = None,
                 scene_turn_includes_presentation: bool = False) -> None:
        if type(turns_per_story_tick) is not int or turns_per_story_tick < 1:
            raise ValueError("turns_per_story_tick must be positive")
        self.store = store
        self.program = program
        self.turn_engine = turn_engine
        self.telemetry = telemetry or LangfuseTelemetry()
        self.turns_per_story_tick = turns_per_story_tick
        if any(step.get("at_subtick", 0) >= turns_per_story_tick for step in program.steps):
            raise ValueError("campaign at_subtick must be smaller than turns_per_story_tick")
        self.clock = StoryClock(program.ticks_per_day * turns_per_story_tick,
                                overnight_requires_rest=True)
        self.scene_presenter = scene_presenter
        self.novel_presenter = novel_presenter
        self.message_batch_writer = message_batch_writer
        self.scene_turn_includes_presentation = scene_turn_includes_presentation
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    def _story_tick(self, snapshot: Snapshot) -> int:
        return snapshot.tick // self.turns_per_story_tick

    def _step_due(self, snapshot: Snapshot, step: dict[str, Any]) -> bool:
        return snapshot.tick >= step["at"] * self.turns_per_story_tick + step.get("at_subtick", 0)

    async def start(self, game_id: str, *, present_opening: bool = True) -> CampaignOutcome:
        async with self._locks.setdefault(game_id, asyncio.Lock()):
            self._validate_snapshot(self.store.load(game_id))
            with self.telemetry.span("campaign-start", {"game_id": game_id, "campaign_id": self.program.program_id},
                                     session_id=session_id_for_game(game_id, self.program.program_id)) as span:
                resumed = await self._flush_ready(game_id)
                self._reconcile_encounters(game_id)
                result = self._drain(game_id, resumed)
                if present_opening:
                    result = await self._present_novel(
                        game_id, "", "campaign:opening", result, None, opening=True,
                    )
                span.update(metadata={"tick": result.snapshot.tick, "gate_id": result.gate_id})
                return result

    async def submit(self, game_id: str, text: str, turn_id: str,
                     progress: TurnProgress | None = None) -> CampaignOutcome:
        if not text.strip() or not turn_id:
            raise ValueError("campaign turn requires text and ID")
        async with self._locks.setdefault(game_id, asyncio.Lock()):
            with self.telemetry.span(
                "campaign-turn", {"game_id": game_id, "turn_id": turn_id, "campaign_id": self.program.program_id},
                input=text if self.telemetry.capture_content else None,
                session_id=session_id_for_game(game_id, self.program.program_id),
            ) as span:
                await emit(progress, "stage", stage="campaign")
                result = await self._submit_locked(game_id, text, turn_id, progress)
                result = await self._present_scene(game_id, text, turn_id, result, progress)
                result = await self._present_novel(game_id, text, turn_id, result, progress)
                if not self.store.event_exists(game_id, f"{turn_id}:completed") and any(
                    self.store.event_exists(game_id, f"{turn_id}:{kind}")
                    for kind in ("continue", "choice", "advance", "input", "query", "action")
                ):
                    before = self.store.load(game_id)
                    after = self.store.commit(
                        game_id, before.version,
                        WorldEvent(f"{turn_id}:completed", "campaign_turn_completed", "player", None,
                                   before.tick, (), {"input": text, "display_text": result.text,
                                                     "display_segments": [part.to_dict() for part in result.segments]}),
                        (), (),
                    )
                    result = replace(result, snapshot=after)
                span.update(metadata={"tick": result.snapshot.tick, "day": result.snapshot.data["campaign"]["day"],
                                      "gate_id": result.gate_id, "complete": result.complete})
                span.metric("story.campaign_day", float(result.snapshot.data["campaign"]["day"]))
                if self.telemetry.capture_content:
                    span.update(output=result.text)
                return result

    async def _present_novel(self, game_id: str, text: str, turn_id: str,
                             result: CampaignOutcome,
                             progress: TurnProgress | None,
                             *, opening: bool = False) -> CampaignOutcome:
        if self.novel_presenter is None:
            return result
        operation = self._committed_operation(game_id, turn_id) if not opening else None
        if not opening and operation is None:
            return result
        if (not opening and self.scene_turn_includes_presentation and operation is not None
                and operation[0] in {"input", "query", "action"}):
            return result
        if (operation is not None and operation[0] == "choice"
                and operation[1].get("single_message_presentation")):
            return result
        event_id = f"{turn_id}:novel"
        saved_details = self.store.event_details(game_id, event_id)
        saved = saved_details.get("text") if saved_details is not None else None
        if saved is not None and not isinstance(saved, str):
            raise ValueError("saved novel presentation is invalid")
        if saved is None and opening and not any(
            part.kind != "prompt" and part.text.strip() for part in result.segments
        ):
            return result
        if saved is None:
            context = SceneContext(
                game_id, text, result.snapshot, result.segments,
                opening or (operation is not None and operation[0] == "choice"
                            and result.snapshot.tick == 0),
                result.snapshot.data["campaign"]["day"], self.clock.period(result.snapshot.tick),
                self.program.current_action_context(result.snapshot).get("goal", ""),
            )
            await emit(progress, "stage", stage="narrating")
            saved = (await self.novel_presenter.present(context)).strip()
            if not saved:
                raise ValueError("novel presenter returned no prose")
            before = self.store.load(game_id)
            self.store.commit(
                game_id, before.version,
                WorldEvent(event_id, "novel_presented", None, turn_id, before.tick, (),
                           {"text": saved}), (), (),
            )
        segments = (StorySegment("narration", saved),) + tuple(
            part for part in result.segments if part.kind == "prompt"
        )
        await emit(progress, "segment", segment=segments[0].to_dict())
        return replace(result, text="\n\n".join(part.text for part in segments),
                       snapshot=self.store.load(game_id), segments=segments)

    async def _present_scene(self, game_id: str, text: str, turn_id: str,
                             result: CampaignOutcome,
                             progress: TurnProgress | None) -> CampaignOutcome:
        if self.scene_presenter is None or result.complete:
            return result
        operation = self._committed_operation(game_id, turn_id)
        if operation is None:
            return result
        kind, _ = operation
        if self.scene_turn_includes_presentation and kind in {"input", "query", "action"}:
            return result
        if kind == "choice" and operation[1].get("single_message_presentation"):
            return result
        if kind == "continue":
            return result
        if kind == "choice" and result.snapshot.tick == 0 and result.gate_id is not None:
            return result
        event_id = f"{turn_id}:scene"
        if self.store.event_exists(game_id, event_id):
            return result
        context = SceneContext(
            game_id, text, result.snapshot, result.segments,
            kind == "choice" and result.snapshot.tick == 0 and result.gate_id is None,
            result.snapshot.data["campaign"]["day"], self.clock.period(result.snapshot.tick),
        )
        await emit(progress, "stage", stage="scene")
        with self.telemetry.span(
            "campaign-scene-presentation",
            {"game_id": game_id, "turn_id": turn_id, "opening": context.opening,
             "tick": result.snapshot.tick,
             "visible_segment_kinds": [part.kind for part in result.segments]},
            kind="agent",
        ) as scene_span:
            prose = (await self.scene_presenter.present(context)).strip()
            if not prose:
                raise ValueError("scene presenter returned no player-visible prose")
            scene_span.metric("story.scene_presented", 1.0)
            if self.telemetry.capture_content:
                scene_span.update(output=prose)
        before = self.store.load(game_id)
        after = self.store.commit(
            game_id, before.version,
            WorldEvent(event_id, "scene_presented", None, turn_id, before.tick, (),
                       {"opening": context.opening}),
            (Observation(f"{event_id}:player", event_id, "player", "scene", prose, before.tick),),
            (),
        )
        parts = list(result.segments)
        insertion = next((index for index, part in enumerate(parts) if part.kind == "prompt"), len(parts))
        segment = StorySegment("scene", prose)
        parts.insert(insertion, segment)
        await emit(progress, "segment", segment=segment.to_dict())
        return replace(result, text="\n\n".join(part.body_text for part in parts if part.text),
                       snapshot=after, segments=tuple(parts))

    async def recover_turn(self, game_id: str, turn_id: str) -> CampaignOutcome | None:
        """Finish a committed turn before a CLI assigns its ID to new input."""
        if self.store.event_exists(game_id, f"{turn_id}:completed"):
            return None
        committed = self._committed_operation(game_id, turn_id)
        if committed is None:
            return None
        _, details = committed
        original = details.get("request_text", details.get("text"))
        if not isinstance(original, str):
            raise ValueError("committed campaign turn has no recoverable input")
        return await self.submit(game_id, original, turn_id)

    def _committed_operation(self, game_id: str, turn_id: str) -> tuple[str, dict[str, object]] | None:
        for kind in ("continue", "choice", "advance", "input", "query", "action"):
            details = self.store.event_details(game_id, f"{turn_id}:{kind}")
            if details is not None:
                return kind, details
        return None

    async def _submit_locked(self, game_id: str, text: str, turn_id: str,
                             progress: TurnProgress | None = None) -> CampaignOutcome:
        snapshot = self.store.load(game_id)
        self._validate_snapshot(snapshot)
        committed = self._committed_operation(game_id, turn_id)
        if committed is not None:
            kind, details = committed
            original = details.get("request_text", details.get("text"))
            if original != text:
                raise ValueError("turn ID already belongs to a different input")
            completed = self.store.event_details(game_id, f"{turn_id}:completed") or {}
            saved_segments = completed.get("display_segments")
            if isinstance(saved_segments, list):
                return self._outcome(game_id, [StorySegment(**item) for item in saved_segments])
            resumed = await self._flush_ready(game_id, progress)
            self._reconcile_encounters(game_id)
            replay = [segment_for_observation(self.store, game_id, item)
                      for item in self.store.observations_for(game_id, "player")
                      if item.event_id.startswith(f"{turn_id}:")]
            parts = resumed + [part for part in replay if part not in resumed]
            if kind == "continue":
                step_id = details.get("step_id")
                start = next((index for index, step in enumerate(self.program.steps)
                              if step["id"] == step_id), -1)
                cursor = self.store.load(game_id).data["campaign"]["cursor"]
                for step in self.program.steps[start + 1:cursor]:
                    event_id = f"campaign:{step['id']}"
                    if step["kind"] in {"scene", "finale"} and self.store.event_exists(game_id, event_id):
                        parts.extend(segment_for_observation(self.store, game_id, item)
                                     for item in self.store.observations_for(game_id, "player")
                                     if item.event_id == event_id)
            self._append_passage(parts, details.get("before_tick"), details.get("duration_ticks"),
                                 details.get("duration", "brief"))
            return self._drain(game_id, parts or ["上次操作已提交，继续当前进度。"])
        if snapshot.data["campaign"]["ending"] is not None:
            raise ValueError("campaign is complete")
        gate = self._due_gate(snapshot)
        if gate is not None:
            if gate["kind"] == "continue":
                if text != "/continue":
                    return self._outcome(game_id, [StorySegment("prompt", self._prompt(gate))])
                self.store.commit(
                    game_id, snapshot.version,
                    WorldEvent(f"{turn_id}:continue", "campaign_continued", "player", None,
                               snapshot.tick, (Effect(("campaign", "cursor"),
                                                      snapshot.data["campaign"]["cursor"] + 1),),
                               {"step_id": gate["id"], "request_text": text}),
                    (), (),
                )
                return self._drain(game_id, [])
            if not text.startswith("/choose "):
                return self._outcome(game_id, [StorySegment("prompt", self._prompt(gate))])
            parts = text.split(maxsplit=2)
            if len(parts) < 2:
                return self._outcome(game_id, [StorySegment("prompt", self._prompt(gate))])
            selected = next((option for option in gate["options"] if option["id"] == parts[1]), None)
            if selected is None:
                return self._outcome(game_id, ["这个选项不可用。", StorySegment("prompt", self._prompt(gate))])
            if gate["kind"] == "message" and selected["id"] != "skip":
                recipient = selected.get("recipient")
                if recipient not in snapshot.data["campaign"]["met"] or not snapshot.data["campaign"]["met"][recipient]:
                    return self._outcome(game_id, ["只能发送给已经互动过的嘉宾。", StorySegment("prompt", self._prompt(gate))])
                if len(parts) < 3 or not parts[2].strip():
                    return self._outcome(game_id, ["请在选项后写留言内容。", StorySegment("prompt", self._prompt(gate))])
            content = parts[2].strip() if len(parts) > 2 else ""
            if gate["kind"] == "message" and self.message_batch_writer is not None:
                await emit(progress, "stage", stage="characters")
            incoming, message_prose = await self._commit_choice(
                snapshot, gate, selected, content, turn_id, text)
            visible = [StorySegment("narration", selected.get("text", f"已选择：{selected['label']}"))]
            if gate["kind"] == "message":
                if selected["id"] == "skip":
                    visible = [StorySegment("narration", "你决定今晚不发送心动留言。")]
                else:
                    visible = [StorySegment("message", f"你发给{selected['label']}的心动留言：{content}")]
                if message_prose:
                    visible.append(StorySegment("narration", message_prose))
            return self._drain(game_id, visible + [StorySegment("message", item) for item in incoming])
        if text == "/continue":
            raise ValueError("no campaign continuation is due")
        if text.startswith("/choose "):
            raise ValueError("no campaign choice is due")
        if text in {"/next", "/rest"}:
            duration = "rest" if text == "/rest" else "standard"
            next_tick = (snapshot.tick + self.clock.elapsed("rest", snapshot.tick)
                         if text == "/rest" else
                         (self._story_tick(snapshot) + 1) * self.turns_per_story_tick)
            cursor = snapshot.data["campaign"]["cursor"]
            if cursor < len(self.program.steps):
                pending = self.program.steps[cursor]
                pending_tick = (pending["at"] * self.turns_per_story_tick
                                + pending.get("at_subtick", 0))
                if snapshot.tick < pending_tick < next_tick:
                    next_tick = pending_tick
                    duration = "standard"
            self.store.commit(game_id, snapshot.version,
                              WorldEvent(f"{turn_id}:advance", "time_advanced", "player", None,
                                         next_tick, (), {"request_text": text,
                                                         "before_tick": snapshot.tick,
                                                         "duration_ticks": next_tick - snapshot.tick,
                                                         "duration": duration}), (), ())
            await emit(progress, "stage", stage="background")
            parts = await self._flush_ready(game_id, progress)
            self._append_passage(parts, snapshot.tick, next_tick - snapshot.tick, duration)
        else:
            if self.turn_engine is None:
                raise ValueError("free-form turns require a scene turn engine")
            cursor = snapshot.data["campaign"]["cursor"]
            pending_tick = None
            if cursor < len(self.program.steps):
                pending = self.program.steps[cursor]
                pending_tick = (pending["at"] * self.turns_per_story_tick
                                + pending.get("at_subtick", 0))
            result = await self.turn_engine.run_turn(
                TurnInput(game_id, text, turn_id, self.turn_engine.package.version),
                max_tick=pending_tick, progress=progress,
            )
            parts = list(getattr(result, "segments", ()))
            if not parts and getattr(result.decision, "duration", "brief") != "rest":
                parts = [StorySegment("narration", result.narration)]
            parts.extend(await self._flush_ready(game_id, progress))
            if result.decision.intent == "speech":
                self._record_encounters(game_id, result.decision.target_ids, turn_id)
            elif result.decision.intent == "action":
                details = self.store.event_details(game_id, f"{turn_id}:action") or {}
                targets = details.get("target_ids", [])
                if isinstance(targets, list):
                    self._record_encounters(game_id, targets, turn_id, affinity=False)
            duration = getattr(result.decision, "duration", "brief")
            if (duration == "rest" and result.snapshot.tick // self.clock.ticks_per_day
                    == snapshot.tick // self.clock.ticks_per_day):
                duration = "standard"
            if not self.scene_turn_includes_presentation:
                self._append_passage(parts, snapshot.tick, result.snapshot.tick - snapshot.tick, duration)
        outcome = self._drain(game_id, parts)
        return (replace(outcome, model_usage=getattr(result, "model_usage", ()))
                if text not in {"/next", "/rest"} else outcome)

    def _append_passage(self, parts: list[DisplayPart], before_tick: object,
                        duration_ticks: object, duration: object) -> None:
        if type(before_tick) is not int or type(duration_ticks) is not int:
            return
        if duration not in {"brief", "standard", "extended", "rest"}:
            duration = "brief"
        passage = self.clock.passage(before_tick, before_tick + duration_ticks, duration)
        if passage:
            parts.append(StorySegment("time", passage))

    async def _flush_ready(self, game_id: str,
                           progress: TurnProgress | None = None) -> list[StorySegment]:
        if not self.store.ready_work(game_id, self.store.load(game_id).tick):
            return []
        visible_before = {item.observation_id for item in self.store.observations_for(game_id, "player")}
        resume = getattr(self.turn_engine, "run_ready_work", None)
        if resume is None:
            raise ValueError("ready background work requires a work runner")
        for _ in range(16):
            if progress is not None:
                await emit(progress, "stage", stage="background")
                await resume(game_id, progress=progress)
            else:
                await resume(game_id)
            if not self.store.ready_work(game_id, self.store.load(game_id).tick):
                return [segment_for_observation(self.store, game_id, item)
                        for item in self.store.observations_for(game_id, "player")
                        if item.observation_id not in visible_before]
        raise RuntimeError("background work budget exhausted; retry this turn ID")

    def _validate_snapshot(self, snapshot: Snapshot) -> None:
        scenario = snapshot.data.get("scenario")
        if not isinstance(scenario, dict) or scenario.get("id") != self.program.program_id:
            raise ValueError("game belongs to another scenario")
        campaign = snapshot.data.get("campaign")
        if not isinstance(campaign, dict) or type(campaign.get("cursor")) is not int:
            raise ValueError("game has no campaign state")
        for work in self.store.pending_work(snapshot.game_id):
            if work.kind == "npc_reply":
                raise ValueError("Unsupported legacy save (npc_reply); delete this save and start a new game.")

    def _record_encounters(self, game_id: str, target_ids: list[str], turn_id: str,
                           *, affinity: bool = True) -> None:
        if self.store.event_exists(game_id, f"{turn_id}:encounter"):
            return
        before = self.store.load(game_id)
        campaign = before.data["campaign"]
        effects: list[Effect] = []
        for actor_id in set(target_ids):
            if actor_id in campaign["met"]:
                effects.append(Effect(("campaign", "met", actor_id), True))
                if affinity:
                    effects.append(Effect(("campaign", "affinity", actor_id),
                                          campaign["affinity"][actor_id] + 1))
        if effects:
            self.store.commit(game_id, before.version,
                              WorldEvent(f"{turn_id}:encounter", "campaign_encounter", "player", turn_id,
                                         before.tick, tuple(effects)), (), ())

    def _reconcile_encounters(self, game_id: str) -> None:
        for item in self.store.player_inputs_for(game_id):
            if (item.channel == "speech" and item.event_id.endswith(":input")) or (
                    item.channel == "action" and item.event_id.endswith(":action")):
                turn_id = item.event_id.rsplit(":", 1)[0]
                if not self.store.event_exists(game_id, f"{turn_id}:encounter"):
                    self._record_encounters(game_id, list(item.target_ids), turn_id,
                                            affinity=item.channel == "speech")

    def _due_gate(self, snapshot: Snapshot) -> dict[str, Any] | None:
        cursor = snapshot.data["campaign"]["cursor"]
        if cursor >= len(self.program.steps):
            return None
        step = self.program.steps[cursor]
        return step if self._step_due(snapshot, step) and step["kind"] in {"continue", "choice", "message"} else None

    @staticmethod
    def _prompt(step: dict[str, Any]) -> str:
        if step["kind"] == "continue":
            return f"{step['prompt']}\n输入 /continue：{step['label']}"
        options = " / ".join(f"{item['id']}={item['label']}" for item in step["options"])
        suffix = "；留言格式：/choose 选项ID 留言内容" if step["kind"] == "message" else "；输入 /choose 选项ID"
        return f"{step['prompt']}\n可选：{options}{suffix}"

    async def _commit_choice(self, before: Snapshot, step: dict[str, Any], option: dict[str, Any],
                             content: str, turn_id: str, request_text: str) -> tuple[list[str], str]:
        campaign = before.data["campaign"]
        effects = [Effect(("campaign", "cursor"), campaign["cursor"] + 1),
                   Effect(("campaign", "choices", step["id"]), option["id"])]
        if step["kind"] == "choice":
            effects.append(Effect(("campaign", "answers", step["id"]), content.strip()))
        observations: list[Observation] = []
        recipient = option.get("recipient") if step["kind"] == "message" and option["id"] != "skip" else None
        affinity_delta: dict[str, int] = {}
        if step["kind"] == "message":
            effects.append(Effect(("campaign", "messages", step["id"]), content if recipient else ""))
            if recipient:
                affinity_delta[recipient] = 1
        if option.get("meet") is not None:
            actor_id = option["meet"]
            if actor_id not in campaign["met"]:
                raise ValueError("choice refers to unknown actor")
            effects.append(Effect(("campaign", "met", actor_id), True))
        for actor_id, delta in option.get("affinity", {}).items():
            affinity_delta[actor_id] = affinity_delta.get(actor_id, 0) + delta
        for actor_id, delta in affinity_delta.items():
            if actor_id not in campaign["affinity"]:
                raise ValueError("choice affinity refers to unknown actor")
            effects.append(Effect(("campaign", "affinity", actor_id), campaign["affinity"][actor_id] + delta))
        event_id = f"{turn_id}:choice"
        player_summary = f"{step['prompt']} 你选择了{option['label']}。"
        if content.strip():
            player_summary += f" 你说：{content.strip()}"
        observations.append(Observation(f"{event_id}:player-choice", event_id, "player",
                                        "campaign_choice", player_summary, before.tick))
        notified = option.get("notify") or option.get("meet")
        if notified:
            observations.append(Observation(f"{event_id}:participant", event_id, notified,
                                            "shared_experience", f"玩家选择与你共同参与：{step['prompt']}", before.tick))
        incoming_texts: list[str] = []
        batch_prose = ""
        batch_messages: dict[str, str] = {}
        if step["kind"] == "message":
            eligible = [rule for rule in step.get("incoming", [])
                        if campaign["met"].get(rule["actor"])
                        and campaign["affinity"].get(rule["actor"], -1)
                        + affinity_delta.get(rule["actor"], 0) >= rule["min_affinity"]]
            if self.message_batch_writer is not None:
                try:
                    batch = await self.message_batch_writer.write(
                        before.game_id, tuple(rule["actor"] for rule in eligible),
                        recipient, content,
                        {rule["actor"]: rule["text"] for rule in eligible},
                    )
                    batch_prose = batch.prose.strip()
                    batch_messages = batch.messages
                except (ExecutionRequestLimitExceeded, ExecutionTimeout):
                    raise
                except Exception as error:
                    with self.telemetry.span("campaign-message-fallback",
                                             {"error_type": type(error).__name__}) as fallback_span:
                        fallback_span.metric("story.campaign_message_fallback", 1.0)
            senders: list[str] = []
            for index, rule in enumerate(step.get("incoming", [])):
                actor_id = rule["actor"]
                score = campaign["affinity"].get(actor_id, -1) + affinity_delta.get(actor_id, 0)
                if not campaign["met"].get(actor_id) or score < rule["min_affinity"]:
                    continue
                senders.append(actor_id)
                message = batch_messages.get(actor_id, rule["text"])
                visible = f"来自{rule['label']}的心动留言：{message}"
                incoming_texts.append(visible)
                observations.append(Observation(f"{event_id}:incoming:{index}:player", event_id,
                                                "player", "private_message", visible, before.tick))
                observations.append(Observation(f"{event_id}:incoming:{index}:sender", event_id,
                                                actor_id, "outgoing_message", message, before.tick))
            effects.append(Effect(("campaign", "incoming", step["id"]), senders))
        if recipient:
            observations.append(Observation(f"{event_id}:recipient", event_id, recipient,
                                            "private_message", content, before.tick))
        event = WorldEvent(event_id, "campaign_choice", "player", None, before.tick,
                           tuple(effects), {"step_id": step["id"], "option_id": option["id"],
                                            "request_text": request_text,
                                            "single_message_presentation":
                                                step["kind"] == "message" and self.message_batch_writer is not None,
                                            "single_message_prose": batch_prose})
        with self.telemetry.span("campaign-choice", {"step_id": step["id"], "option_id": option["id"],
                                                      "recipient_id": recipient}) as span:
            self.store.commit(before.game_id, before.version, event, tuple(observations), ())
            span.metric("story.campaign_choice", 1.0)
            span.metric("story.incoming_messages", float(len(incoming_texts)))
        return incoming_texts, batch_prose

    def _drain(self, game_id: str, parts: list[DisplayPart]) -> CampaignOutcome:
        while True:
            before = self.store.load(game_id)
            campaign = before.data["campaign"]
            story_tick = self._story_tick(before)
            day = max(campaign["day"], min(story_tick // self.program.ticks_per_day + 1,
                                           self.program.final_tick // self.program.ticks_per_day + 1))
            if campaign["day"] != day:
                event = WorldEvent(f"campaign:day:{day}", "campaign_day", None, None,
                                   before.tick, (Effect(("campaign", "day"), day),))
                self.store.commit(game_id, before.version, event, (), ())
                continue
            cursor = campaign["cursor"]
            if cursor >= len(self.program.steps):
                break
            step = self.program.steps[cursor]
            if not self._step_due(before, step):
                break
            if step["kind"] in {"continue", "choice", "message"}:
                parts.append(StorySegment("prompt", self._prompt(step)))
                break
            effects = [Effect(("campaign", "cursor"), cursor + 1)]
            event_id = f"campaign:{step['id']}"
            if step["kind"] == "scene":
                for actor_id, location in step.get("locations", {}).items():
                    effects.append(Effect(("actors", actor_id, "location"), location))
                for actor_id, delta in step.get("affinity", {}).items():
                    effects.append(Effect(("campaign", "affinity", actor_id), campaign["affinity"][actor_id] + delta))
                text = step["text"]
                for choice_id in step.get("reveal_answers", []):
                    answer = campaign["answers"].get(choice_id)
                    if answer:
                        text += f"\n{answer}"
            else:
                target = campaign["choices"].get(step["choice_key"])
                paired = target in campaign["affinity"] and campaign["affinity"][target] >= step["threshold"]
                ending = f"paired:{target}" if paired else "solo"
                effects.append(Effect(("campaign", "ending"), ending))
                if paired:
                    text = step["success_text"]
                elif target in campaign["affinity"]:
                    text = step.get("rejected_text", step["other_text"])
                else:
                    text = step["other_text"]
                if paired and campaign["choices"].get(step.get("bonus_choice_key")) == target:
                    text += "\n" + step.get("bonus_text", "")
            event = WorldEvent(event_id, f"campaign_{step['kind']}", None, None, before.tick,
                               tuple(effects), {"step_id": step["id"]})
            observations = [Observation(f"{event_id}:player", event_id, "player", "scene", text, before.tick)]
            if step["kind"] == "scene":
                for index, item in enumerate(step.get("observations", [])):
                    observations.append(Observation(f"{event_id}:private:{index}", event_id,
                                                    item["recipient"], "background", item["text"], before.tick))
            with self.telemetry.span("campaign-step", {"step_id": step["id"], "kind": step["kind"],
                                                      "tick": before.tick, "recipient_ids":
                                                      [item.recipient_id for item in observations]}) as span:
                self.store.commit(game_id, before.version, event, tuple(observations), ())
                span.metric("story.campaign_step", 1.0)
            parts.append(StorySegment("scene", text))
        return self._outcome(game_id, parts)

    def _outcome(self, game_id: str, parts: list[DisplayPart]) -> CampaignOutcome:
        snapshot = self.store.load(game_id)
        gate = self._due_gate(snapshot)
        segments = tuple(part if isinstance(part, StorySegment) else StorySegment("narration", part)
                         for part in parts if part)
        return CampaignOutcome("\n\n".join(part.body_text for part in segments if part.text), snapshot,
                               gate["id"] if gate else None,
                               snapshot.data["campaign"]["ending"] is not None, segments,
                               self.clock.period(snapshot.tick))
