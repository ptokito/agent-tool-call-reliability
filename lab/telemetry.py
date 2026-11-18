"""OpenTelemetry setup.

Spans are always captured in memory so every run's trace is saved with the
raw results. If OTEL_EXPORTER_OTLP_ENDPOINT is set (for example
http://localhost:4318 for Jaeger), spans are also exported over OTLP/HTTP.
"""
import os

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


def setup(service_name="agent-toolcall-lab"):
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    memory = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    otlp = bool(os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"))
    if otlp:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    return provider, memory, otlp


def span_to_dict(s):
    ctx = s.get_span_context()
    return {
        "name": s.name,
        "trace_id": format(ctx.trace_id, "032x"),
        "span_id": format(ctx.span_id, "016x"),
        "parent_span_id": format(s.parent.span_id, "016x") if s.parent else None,
        "status": s.status.status_code.name,
        "status_description": s.status.description,
        "attributes": dict(s.attributes or {}),
        "events": [e.name for e in s.events],
        "duration_ms": round((s.end_time - s.start_time) / 1e6, 2) if s.end_time else None,
    }
