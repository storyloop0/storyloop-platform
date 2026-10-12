"""Offline checks for exception handling at the Langfuse/OTel boundary."""

import asyncio
from unittest.mock import patch

import pytest
from langfuse import Langfuse
from opentelemetry.trace import StatusCode
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from storyloop_platform.adapters.telemetry import LangfuseTelemetry


def _offline_telemetry(*, capture_content: bool) -> tuple[LangfuseTelemetry, InMemorySpanExporter, TracerProvider]:
    """Use real SDK context managers without initializing transport or workers."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    client = object.__new__(Langfuse)
    client._otel_tracer = provider.get_tracer("offline-exception-test")
    client._environment = "offline"
    client._release = None
    client._mask = None
    client._resources = None
    client._tracing_enabled = True
    return LangfuseTelemetry(client, capture_content=capture_content), exporter, provider


def test_disabled_capture_records_safe_error_and_propagates_original() -> None:
    telemetry, exporter, provider = _offline_telemetry(capture_content=False)
    private_marker = "SYNTHETIC_PRIVATE_INPUT_MARKER"
    failure = ValueError(private_marker)
    try:
        with pytest.raises(ValueError) as caught:
            with telemetry.span("parent", {"stage": "turn", "game_id": "g1"}):
                with telemetry.span("child", {"stage": "generation", "work_id": "w1"}):
                    raise failure

        assert caught.value is failure
        spans = exporter.get_finished_spans()
        assert {span.name for span in spans} == {"parent", "child"}
        for span in spans:
            assert span.status.status_code is StatusCode.ERROR
            assert span.status.description == "ValueError"
            assert span.attributes["langfuse.observation.status_message"] == "ValueError"
            assert private_marker not in str((span.attributes, span.events, span.status))
            assert not any(event.name == "exception" for event in span.events)

        with telemetry.span("success", {"stage": "generation"}):
            pass
        success = exporter.get_finished_spans()[-1]
        assert success.name == "success"
        assert success.status.status_code is not StatusCode.ERROR
    finally:
        provider.shutdown()


def test_enabled_capture_keeps_detailed_exception_recording() -> None:
    telemetry, exporter, provider = _offline_telemetry(capture_content=True)
    private_marker = "SYNTHETIC_PRIVATE_INPUT_MARKER"
    failure = ValueError(private_marker)
    try:
        with pytest.raises(ValueError) as caught:
            with telemetry.span("generation", {"stage": "generation"}):
                raise failure

        assert caught.value is failure
        span = exporter.get_finished_spans()[0]
        assert span.status.status_code is StatusCode.ERROR
        assert private_marker in span.status.description
        assert any(event.name == "exception" and private_marker in str(event.attributes)
                   for event in span.events)
    finally:
        provider.shutdown()


def test_disabled_capture_sanitizes_cancellation_and_propagates_it() -> None:
    telemetry, exporter, provider = _offline_telemetry(capture_content=False)
    private_marker = "SYNTHETIC_PRIVATE_INPUT_MARKER"
    cancellation = asyncio.CancelledError(private_marker)
    try:
        with pytest.raises(asyncio.CancelledError) as caught:
            with telemetry.span("generation", {"stage": "generation"}):
                raise cancellation

        assert caught.value is cancellation
        span = exporter.get_finished_spans()[0]
        assert span.status.status_code is StatusCode.ERROR
        assert span.status.description == "CancelledError"
        assert span.attributes["langfuse.observation.status_message"] == "CancelledError"
        assert private_marker not in str((span.attributes, span.events, span.status))
    finally:
        provider.shutdown()


def test_telemetry_status_failures_do_not_replace_application_exception() -> None:
    class BrokenSpan:
        def update(self, **kwargs: object) -> None:
            raise RuntimeError("telemetry update failed")

    class Context:
        exit_args: tuple[object, object, object] | None = None

        def __enter__(self) -> BrokenSpan:
            return BrokenSpan()

        def __exit__(self, *args: object) -> None:
            self.exit_args = args

    class Client:
        def __init__(self) -> None:
            self.context = Context()

        def start_as_current_observation(self, **kwargs: object) -> Context:
            return self.context

    client = Client()
    attributes_context = Context()
    failure = ValueError("SYNTHETIC_PRIVATE_INPUT_MARKER")
    with patch("langfuse.propagate_attributes", return_value=attributes_context), \
            patch("opentelemetry.trace.get_current_span", side_effect=RuntimeError("status failed")):
        with pytest.raises(ValueError) as caught:
            with LangfuseTelemetry(client, capture_content=False).span(
                "generation", {}, session_id="safe-session"
            ):
                raise failure

    assert caught.value is failure
    assert client.context.exit_args == (None, None, None)
    assert attributes_context.exit_args == (None, None, None)
