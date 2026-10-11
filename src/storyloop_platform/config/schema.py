"""Validated deployment settings, independent of model and storage SDKs."""

from __future__ import annotations

import re
from decimal import Decimal
from ipaddress import IPv4Address, IPv6Address
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

Name = Annotated[str, Field(min_length=1, pattern=r"\S")]
EnvName = Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]
PositiveInt = Annotated[int, Field(gt=0)]
Seconds = Annotated[float, Field(gt=0, allow_inf_nan=False)]
Amount = Annotated[Decimal, Field(ge=0, allow_inf_nan=False, strict=False)]
Task = Literal[
    "single_turn", "adjudication", "narration", "prologue", "followup_actions"
]


class SettingsModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, hide_input_in_errors=True
    )


class ProviderSettings(SettingsModel):
    base_url: Annotated[str, Field(pattern=r"^https?://[^\s/?#]+(?:/[^\s?#]*)?$")]
    api_key_env: EnvName
    base_url_env: EnvName | None = None

    @field_validator("base_url")
    @classmethod
    def valid_endpoint(cls, value: str) -> str:
        # urlsplit parses authorities permissively and its errors can echo input.
        try:
            parsed = urlsplit(value)
            host, port = parsed.hostname, parsed.port
            if parsed.username is not None:
                raise ValueError("credentials")
            if not host or port == 0:
                raise ValueError("host or port")
            authority = parsed.netloc
            if authority.startswith("["):
                IPv6Address(host)
                suffix = authority[authority.index("]") + 1 :]
            else:
                suffix = ":" + authority.split(":", 1)[1] if ":" in authority else ""
                if re.fullmatch(r"[0-9.]+", host):
                    IPv4Address(host)
                elif len(host) > 253 or any(
                    not re.fullmatch(
                        r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label
                    )
                    for label in host.removesuffix(".").split(".")
                ):
                    raise ValueError("host")
            if suffix and not re.fullmatch(r":[0-9]+", suffix):
                raise ValueError("port")
        except ValueError:
            raise ValueError(
                "provider base_url requires a valid host and port without credentials"
            ) from None
        return value


class GenerationSettings(SettingsModel):
    temperature: Annotated[float, Field(ge=0, le=2, allow_inf_nan=False)] | None = None
    top_p: Annotated[float, Field(gt=0, le=1, allow_inf_nan=False)] | None = None
    max_tokens: PositiveInt | None = None
    thinking_enable: bool | None = None
    reasoning_effort: (
        Literal["none", "minimal", "low", "medium", "high", "xhigh"] | None
    ) = None
    parallel_tool_calls: bool | None = None


class RateSettings(SettingsModel):
    pricing_version: Name
    input_rmb_per_million: Annotated[
        Decimal, Field(gt=0, allow_inf_nan=False, strict=False)
    ]
    output_rmb_per_million: Annotated[
        Decimal, Field(gt=0, allow_inf_nan=False, strict=False)
    ]
    cached_input_rmb_per_million: Amount
    multiplier: Annotated[Decimal, Field(gt=0, allow_inf_nan=False, strict=False)] = (
        Decimal("1")
    )


class ModelSettings(SettingsModel):
    provider: Name
    model: Name
    timeout_seconds: Seconds = 90
    connect_timeout_seconds: Seconds = 10
    max_retries: Annotated[int, Field(ge=0, le=5)] = 2
    rate: RateSettings | None = None

    @model_validator(mode="after")
    def timeout_order(self) -> Self:
        if self.connect_timeout_seconds > self.timeout_seconds:
            raise ValueError("connect_timeout_seconds cannot exceed timeout_seconds")
        return self


class ChatModelSettings(ModelSettings):
    kind: Literal["chat"] = "chat"
    generation: GenerationSettings = Field(default_factory=GenerationSettings)
    extra_body: dict[str, object] = Field(default_factory=dict, repr=False)
    context_window_tokens: Annotated[int, Field(ge=1024)] = 65536
    tool_choice_policy: Literal["native", "auto_only"] = "native"
    structured_output_transport: Literal["auto", "tool_call"] = "auto"


class EmbeddingModelSettings(ModelSettings):
    kind: Literal["embedding"] = "embedding"
    dimensions: PositiveInt


def _validate_model_kind(value: object) -> object:
    # Pydantic's union_tag_invalid message embeds the raw discriminator value,
    # even with hide_input_in_errors. Reject it before union dispatch instead.
    kind = value.get("kind") if isinstance(value, dict) else getattr(value, "kind", None)
    if kind not in ("chat", "embedding"):
        raise ValueError("model kind must be chat or embedding")
    return value


ModelProfile = Annotated[
    ChatModelSettings | EmbeddingModelSettings,
    Field(discriminator="kind"),
    BeforeValidator(_validate_model_kind),
]


class RuntimeSettings(SettingsModel):
    max_steps: PositiveInt = 8
    max_npc_replies: PositiveInt = 3
    followup_timeout_seconds: Seconds = 12
    context_window_tokens: Annotated[int, Field(ge=1024)] = 65536


class StorageSettings(SettingsModel):
    driver: Literal["sqlite", "postgresql"]
    path: Name | None = None
    path_base: Literal["config", "cwd"] = "config"
    url_env: EnvName | None = None


class HttpSettings(SettingsModel):
    allowed_hosts_env: EnvName | None = None
    allowed_origins_env: EnvName | None = None


class PlayerMemorySettings(SettingsModel):
    driver: Literal["none", "mem0"] = "none"
    enabled_env: EnvName | None = None
    path_env: EnvName | None = None
    extraction_profile: Name | None = None
    embedding_profile: Name | None = None


class CreditsSettings(SettingsModel):
    welcome_points: Annotated[int, Field(ge=0)]
    points_per_rmb: PositiveInt


class PlatformSettings(SettingsModel):
    environment: Literal["local", "online"]
    providers: dict[Name, ProviderSettings]
    models: dict[Name, ModelProfile]
    routes: dict[Task, Name]
    runtime: RuntimeSettings
    storage: StorageSettings
    http: HttpSettings = Field(default_factory=HttpSettings)
    player_memory: PlayerMemorySettings = Field(default_factory=PlayerMemorySettings)
    credits: CreditsSettings | None = None

    @model_validator(mode="after")
    def references(self) -> Self:
        for name, profile in self.models.items():
            if profile.provider not in self.providers:
                raise ValueError(
                    f"models.{name}.provider references an unknown provider"
                )
        for task in ("single_turn", "narration", "followup_actions"):
            if task not in self.routes:
                raise ValueError(f"routes.{task} is required")
        for task, name in self.routes.items():
            if not isinstance(self.models.get(name), ChatModelSettings):
                raise ValueError(f"routes.{task} must reference a chat profile")
            if self.credits is not None and self.models[name].rate is None:
                raise ValueError(
                    f"models.{name}.rate is required when credits are enabled"
                )
        profile = self.model_for("single_turn")
        output = 4096 if profile.generation.max_tokens is None else profile.generation.max_tokens
        window = min(profile.context_window_tokens, self.runtime.context_window_tokens)
        if output + 256 >= window:
            raise ValueError(
                f"routes.single_turn generation.max_tokens {output} plus safety margin 256 "
                f"must be below effective context window {window}; increase model/runtime "
                "context_window_tokens or reduce single_turn generation.max_tokens"
            )
        memory = self.player_memory
        for field, expected in [
            ("extraction_profile", ChatModelSettings),
            ("embedding_profile", EmbeddingModelSettings),
        ]:
            name = getattr(memory, field)
            if (name is not None or memory.driver == "mem0") and not isinstance(
                self.models.get(name), expected
            ):
                raise ValueError(
                    f"player_memory.{field} references a missing or wrong-kind profile"
                )
        extraction = self.models.get(memory.extraction_profile)
        if isinstance(extraction, ChatModelSettings):
            if (
                extraction.extra_body
                or extraction.generation.thinking_enable is not None
                or extraction.generation.parallel_tool_calls is not None
            ):
                raise ValueError(
                    "player_memory.extraction_profile uses generation fields unsupported by Mem0"
                )
        rates = {}
        for name, profile in self.models.items():
            if profile.model in rates and rates[profile.model] != profile.rate:
                raise ValueError(
                    f"models.{name}.rate conflicts for identical reported model names"
                )
            rates[profile.model] = profile.rate
        if self.environment == "local" and (
            self.storage.driver != "sqlite" or self.storage.path is None
        ):
            raise ValueError("local storage requires sqlite and storage.path")
        if self.environment == "online":
            if self.storage.driver != "postgresql" or self.storage.url_env is None:
                raise ValueError(
                    "online storage requires postgresql and storage.url_env"
                )
            if (
                self.http.allowed_hosts_env is None
                or self.http.allowed_origins_env is None
            ):
                raise ValueError(
                    "online http requires allowed_hosts_env and allowed_origins_env"
                )
            if memory.driver == "mem0" and memory.path_env is None:
                raise ValueError("online player_memory requires path_env")
        return self

    def model_for(self, task: str) -> ChatModelSettings:
        if task not in self.routes:
            raise ValueError("unconfigured model task")
        return self.models[self.routes[task]]

    def context_window_for(self, task: str) -> int:
        return self.model_for(task).context_window_tokens
