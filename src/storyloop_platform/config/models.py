"""Late-bound provider credentials and model construction."""

from __future__ import annotations

import os
import json
from collections.abc import Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentscope.model import OpenAIChatModel

    from storyloop_harness.telemetry import Telemetry
from .schema import ChatModelSettings, EmbeddingModelSettings, PlatformSettings


class ModelFactory:
    def __init__(
        self,
        settings: PlatformSettings,
        *,
        env: Mapping[str, str] | None = None,
        telemetry: Telemetry | None = None,
    ) -> None:
        self.settings = settings
        self._env = os.environ if env is None else env
        self.telemetry = telemetry

    def api_key(self, provider: str) -> str:
        return self._env.get(self.settings.providers[provider].api_key_env, "").strip()

    def base_url(self, provider: str) -> str:
        definition = self.settings.providers[provider]
        value = (
            self._env.get(definition.base_url_env or "", "").strip()
            or definition.base_url
        )
        return (
            type(definition)
            .model_validate({**definition.model_dump(), "base_url": value})
            .base_url
        )

    def require_credentials(self) -> None:
        profiles = set(self.settings.routes.values())
        if self.settings.player_memory.driver == "mem0":
            profiles.update(
                (
                    self.settings.player_memory.extraction_profile,
                    self.settings.player_memory.embedding_profile,
                )
            )
        for name in sorted(profiles):
            self._credential(self.settings.models[name].provider)

    def _credential(self, provider: str) -> str:
        key = self.api_key(provider)
        if not key:
            raise ValueError(
                f"set {self.settings.providers[provider].api_key_env} before model use"
            )
        return key

    def create_model(
        self, task: str, *, temperature: float | None = None
    ) -> OpenAIChatModel:
        self.settings.model_for(task)
        return self.create_profile(
            self.settings.routes[task], task=task, temperature=temperature
        )

    def create_profile(
        self, profile: str, *, task: str = "model", temperature: float | None = None
    ) -> OpenAIChatModel:
        definition = self.settings.models.get(profile)
        if not isinstance(definition, ChatModelSettings):
            raise ValueError("model profile must reference a chat model")
        data = definition.model_dump(mode="json")
        if temperature is not None:
            data["generation"]["temperature"] = temperature
        captured = ChatModelSettings.model_validate(data)
        return self.create_resolved_model(
            task=task, model_config_json=captured.model_dump_json(),
            base_url=self.base_url(captured.provider),
        )

    def create_resolved_model(
        self, *, task: str, model_config_json: str, base_url: str
    ) -> OpenAIChatModel:
        """Create the existing adapter using captured profile and endpoint terms."""
        definition = ChatModelSettings.model_validate(json.loads(model_config_json))
        key = self._credential(definition.provider)
        from agentscope.credential import OpenAICredential
        from agentscope.model import OpenAIChatModel
        from httpx2 import Timeout
        from storyloop_harness.generation import (
            CompatibleOpenAIChatModel,
            GenerationBudget,
            ThinkingSafeOpenAIChatFormatter,
        )

        generation = definition.generation.model_dump(exclude_none=True)
        return CompatibleOpenAIChatModel(
            credential=OpenAICredential(
                api_key=key, base_url=base_url
            ),
            model=definition.model,
            stream=False,
            max_retries=definition.max_retries,
            client_kwargs={
                "timeout": Timeout(
                    definition.timeout_seconds,
                    connect=definition.connect_timeout_seconds,
                ),
                "max_retries": definition.max_retries,
            },
            parameters=OpenAIChatModel.Parameters(**generation),
            extra_body=definition.extra_body or None,
            formatter=ThinkingSafeOpenAIChatFormatter(),
            tool_choice_policy=definition.tool_choice_policy,
            structured_output_transport=definition.structured_output_transport,
            generation_budget=GenerationBudget(
                max_requests=definition.generation_max_requests,
                timeout_seconds=definition.generation_timeout_seconds,
            ),
            telemetry=self.telemetry,
            task=task,
        )

    def memory_config(self) -> dict:
        memory = self.settings.player_memory
        extraction = self.settings.models.get(memory.extraction_profile)
        embedding = self.settings.models.get(memory.embedding_profile)
        if not isinstance(extraction, ChatModelSettings) or not isinstance(
            embedding, EmbeddingModelSettings
        ):
            raise ValueError("player memory requires extraction and embedding profiles")
        generation = extraction.generation.model_dump(exclude_none=True)
        llm = {
            "model": extraction.model,
            "api_key": self._credential(extraction.provider),
            "openai_base_url": self.base_url(extraction.provider),
        }
        for field in ("temperature", "max_tokens", "top_p", "reasoning_effort"):
            if field in generation:
                llm[field] = generation.pop(field)
        return {
            "llm": {"provider": "openai", "config": llm},
            "embedder": {
                "provider": "openai",
                "config": {
                    "model": embedding.model,
                    "api_key": self._credential(embedding.provider),
                    "openai_base_url": self.base_url(embedding.provider),
                    "embedding_dims": embedding.dimensions,
                },
            },
        }
