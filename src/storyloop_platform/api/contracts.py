"""Canonical player response schemas, independent of the gameplay runtime."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator


class Contract(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class StorySegment(Contract):
    kind: Literal["narration", "scene", "dialogue", "message", "prompt", "time"]
    text: str
    # Omitted fields have an unvalidated default; explicitly supplied null is invalid.
    # Always serialize with exclude_unset=True to retain the original key presence.
    speaker_id: str = Field(default=None)
    speaker_name: str = Field(default=None)


class ActionOption(Contract):
    label: str
    input: str


class InteractionOption(Contract):
    id: str
    label: str
    enabled: bool
    requires_text: bool


class Interaction(Contract):
    id: str
    kind: Literal["choice", "message", "continue"]
    prompt: str
    options: list[InteractionOption]
    label: str = Field(default=None)


class StatusField(Contract):
    id: str
    label: str
    value: str | int | bool
    min: int = Field(default=None)
    max: int = Field(default=None)


class TurnBilling(Contract):
    charged_milli_points: int
    charged_points: str
    usage_cost_milli_points: int
    balance_milli_points: int
    balance_points: str
    input_tokens: int
    output_tokens: int
    model_calls: int


class View(Contract):
    game_id: str
    catalog_id: str
    mode: Literal["campaign", "freeform"]
    presentation_mode: Literal["interactive", "novel"]
    opening: str
    body: str
    segments: list[StorySegment]
    suggestions: list[str]
    action_options: list[ActionOption]
    status_fields: list[StatusField]
    interaction: Interaction | None
    tick: int
    state_version: int
    day: int | None
    time_of_day: str | None
    complete: bool
    turn_id: str | None
    billing: TurnBilling = Field(default=None)


class HistoryTurn(Contract):
    request_id: str
    input: str | None
    response: View


class History(Contract):
    game_id: str
    intro: View
    turns: list[HistoryTurn]


class TurnFailure(Contract):
    code: str
    message: str
    retryable: bool
    commit_state: Literal["not_started", "committed", "unknown"]
    request_id: str | None
    detail: str = Field(default=None)


class StageEvent(Contract):
    type: Literal["stage"]
    stage: str


class SegmentEvent(Contract):
    type: Literal["segment"]
    segment: StorySegment


class PreviewEvent(Contract):
    type: Literal["preview"]
    body: str
    segments: list[StorySegment]


class CompleteEvent(Contract):
    type: Literal["complete"]
    view: View


class ErrorEvent(TurnFailure):
    type: Literal["error"]


TurnStreamEvent = Annotated[
    StageEvent | SegmentEvent | PreviewEvent | CompleteEvent | ErrorEvent,
    Field(discriminator="type"),
]
stream_event_adapter = TypeAdapter(TurnStreamEvent)


class RecoveryStatusContract(Contract):
    @field_validator("can_recover", mode="before", check_fields=False)
    @classmethod
    def strict_boolean(cls, value):
        if type(value) is not bool:
            raise ValueError("can_recover must be boolean")
        return value


class CompleteTurnRecoveryStatus(RecoveryStatusContract):
    request_id: str
    status: Literal["complete"]
    can_recover: Literal[True]
    reason_code: Literal["RECEIPT_READY"]


class RecoverableTurnRecoveryStatus(RecoveryStatusContract):
    request_id: str
    status: Literal["recoverable"]
    can_recover: Literal[True]
    reason_code: Literal["PREPARED_READY", "SCENE_READY"]


class BlockedTurnRecoveryStatus(RecoveryStatusContract):
    request_id: str
    status: Literal["blocked"]
    can_recover: Literal[False]
    reason_code: Literal["INPUT_MISMATCH", "LATER_REQUEST", "EVIDENCE_INVALID",
                         "CONFIG_CHANGED", "USAGE_UNAVAILABLE", "LEGACY_REQUEST"]


class UnavailableTurnRecoveryStatus(RecoveryStatusContract):
    request_id: str
    status: Literal["not_available"]
    can_recover: Literal[False]
    reason_code: Literal["NO_SNAPSHOT", "UNSUPPORTED"]


TurnRecoveryStatus = Annotated[
    CompleteTurnRecoveryStatus | RecoverableTurnRecoveryStatus |
    BlockedTurnRecoveryStatus | UnavailableTurnRecoveryStatus,
    Field(discriminator="status"),
]
turn_recovery_status_adapter = TypeAdapter(TurnRecoveryStatus)


def contract_schemas() -> dict[str, dict]:
    """Standard JSON Schema roots consumed by the maintained frontend generators."""
    return {"View": View.model_json_schema(), "History": History.model_json_schema(),
            "TurnFailure": TurnFailure.model_json_schema(),
            "TurnStreamEvent": stream_event_adapter.json_schema(),
            "TurnRecoveryStatus": turn_recovery_status_adapter.json_schema()}
