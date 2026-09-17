"""OpenTelemetry wiring. Spans are best effort; the audit table is the durable record."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Span, Status, StatusCode

from ..config import Config, get_config

_tracer: trace.Tracer | None = None
_configured = False


def setup(service_name: str, cfg: Config | None = None) -> None:
    global _tracer, _configured
    if _configured:
        return
    cfg = cfg or get_config()
    _configured = True
    if not cfg.obs.enabled:
        _tracer = trace.get_tracer(service_name)
        return
    try:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

        provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=cfg.obs.otlp_endpoint, insecure=True))
        )
        trace.set_tracer_provider(provider)
    except Exception:
        pass  # collector down: keep running, the audit log still records everything
    _tracer = trace.get_tracer(service_name)


def tracer() -> trace.Tracer:
    global _tracer
    if _tracer is None:
        _tracer = trace.get_tracer("agentd")
    return _tracer


@contextmanager
def span(name: str, attributes: dict[str, Any] | None = None):
    with tracer().start_as_current_span(name) as s:
        for k, v in (attributes or {}).items():
            if v is not None:
                s.set_attribute(k, v)
        yield s


def record_exception(s: Span, exc: BaseException) -> None:
    try:
        s.record_exception(exc)
        s.set_status(Status(StatusCode.ERROR, str(exc)))
    except Exception:
        pass


def current_ids() -> dict[str, str | None]:
    """trace/span ids for the audit row, so Jaeger and Postgres can be cross-referenced."""
    ctx = trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return {"trace_id": None, "span_id": None}
    return {"trace_id": format(ctx.trace_id, "032x"), "span_id": format(ctx.span_id, "016x")}


def gen_ai_attrs(role: str, model: str, usage: dict[str, int] | None = None) -> dict[str, Any]:
    attrs: dict[str, Any] = {
        "gen_ai.system": "openai_compatible",
        "gen_ai.request.model": model,
        "agentd.role": role,
    }
    if usage:
        attrs["gen_ai.usage.input_tokens"] = usage.get("input_tokens", 0)
        attrs["gen_ai.usage.output_tokens"] = usage.get("output_tokens", 0)
    return attrs
