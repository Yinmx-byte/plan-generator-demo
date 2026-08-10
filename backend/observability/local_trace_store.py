"""Bounded, sanitized in-memory trace storage for the business frontend."""

from __future__ import annotations

import os
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from typing import Any, Iterator, Mapping

from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor, TracerProvider


_SAFE_ATTRIBUTE_KEYS = {
    "app.route",
    "app.session_id",
    "gen_ai.operation.name",
    "gen_ai.provider.name",
    "gen_ai.request.model",
    "gen_ai.response.finish_reasons",
    "gen_ai.usage.input_tokens",
    "gen_ai.usage.output_tokens",
    "gen_ai.agent.name",
    "gen_ai.tool.name",
    "agentscope.format.target",
    "agentscope.format.count",
    "agentscope.function.name",
}


def _identifier(value: int, width: int) -> str:
    return f"{value:0{width}x}" if value else ""


def _safe_value(value: Any) -> str | int | float | bool | list[str]:
    if isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (tuple, list)):
        return [str(item)[:120] for item in value[:8]]
    return str(value)[:200]


def _safe_attributes(attributes: Mapping[str, Any] | None) -> dict[str, Any]:
    if not attributes:
        return {}
    return {
        key: _safe_value(value)
        for key, value in attributes.items()
        if key in _SAFE_ATTRIBUTE_KEYS
    }


def _span_category(name: str, attributes: Mapping[str, Any]) -> str:
    operation = str(attributes.get("gen_ai.operation.name") or "").lower()
    function_name = str(attributes.get("agentscope.function.name") or "").lower()
    combined = f"{name} {operation} {function_name}".lower()
    if name.startswith("plan-generator."):
        return "workflow"
    if attributes.get("gen_ai.tool.name") or "tool" in combined:
        return "tool"
    if attributes.get("gen_ai.agent.name") or "agent" in combined or "reply" in combined:
        return "agent"
    if "format" in combined:
        return "formatter"
    if any(word in combined for word in ("rag", "knowledge", "retriev", "embedding")):
        return "retrieval"
    if attributes.get("gen_ai.request.model") or any(word in combined for word in ("chat", "llm", "model")):
        return "model"
    return "workflow"


def _span_payload(span: Span | ReadableSpan, *, running: bool) -> dict[str, Any]:
    context = span.get_span_context() if isinstance(span, Span) else span.context
    parent = getattr(span, "parent", None)
    attributes = _safe_attributes(getattr(span, "attributes", None))
    start_ns = int(getattr(span, "start_time", 0) or 0)
    end_ns = int(getattr(span, "end_time", 0) or 0)
    status = getattr(getattr(span, "status", None), "status_code", None)
    status_name = getattr(status, "name", "UNSET").lower()
    if running:
        status_name = "running"
    duration_ms = max(0.0, (end_ns - start_ns) / 1_000_000) if end_ns else None
    return {
        "trace_id": _identifier(context.trace_id, 32),
        "span_id": _identifier(context.span_id, 16),
        "parent_span_id": _identifier(getattr(parent, "span_id", 0), 16),
        "name": str(getattr(span, "name", "unknown"))[:180],
        "category": _span_category(str(getattr(span, "name", "")), attributes),
        "status": status_name,
        "start_time_ms": round(start_ns / 1_000_000, 3) if start_ns else None,
        "end_time_ms": round(end_ns / 1_000_000, 3) if end_ns else None,
        "duration_ms": round(duration_ms, 2) if duration_ms is not None else None,
        "attributes": attributes,
    }


class LocalTraceStore:
    """Keep recent sanitized traces without introducing a database dependency."""

    def __init__(self, max_traces: int = 80, ttl_seconds: int = 3600) -> None:
        self.max_traces = max(10, max_traces)
        self.ttl_seconds = max(300, ttl_seconds)
        self._lock = threading.RLock()
        self._traces: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def upsert(self, span: Span | ReadableSpan, *, running: bool) -> None:
        payload = _span_payload(span, running=running)
        trace_id = payload["trace_id"]
        if not trace_id:
            return
        now = time.time()
        with self._lock:
            self._prune(now)
            trace_data = self._traces.setdefault(
                trace_id,
                {"trace_id": trace_id, "updated_at": now, "spans": {}},
            )
            trace_data["updated_at"] = now
            trace_data["spans"][payload["span_id"]] = payload
            self._traces.move_to_end(trace_id)
            while len(self._traces) > self.max_traces:
                self._traces.popitem(last=False)

    def get(self, trace_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._prune(time.time())
            trace_data = self._traces.get(trace_id)
            if not trace_data:
                return None
            spans = sorted(
                (dict(span) for span in trace_data["spans"].values()),
                key=lambda item: (item.get("start_time_ms") or 0, item["span_id"]),
            )
        input_tokens = sum(
            int(span["attributes"].get("gen_ai.usage.input_tokens") or 0)
            for span in spans
        )
        output_tokens = sum(
            int(span["attributes"].get("gen_ai.usage.output_tokens") or 0)
            for span in spans
        )
        statuses = {span["status"] for span in spans}
        overall_status = "error" if "error" in statuses else "running" if "running" in statuses else "ok"
        root_spans = [span for span in spans if not span.get("parent_span_id")]
        duration_ms = max(
            (float(span.get("duration_ms") or 0) for span in root_spans),
            default=0.0,
        )
        return {
            "trace_id": trace_id,
            "status": overall_status,
            "span_count": len(spans),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "duration_ms": round(duration_ms, 2),
            "spans": spans,
        }

    def _prune(self, now: float) -> None:
        expired = [
            trace_id
            for trace_id, trace_data in self._traces.items()
            if now - float(trace_data.get("updated_at") or 0) > self.ttl_seconds
        ]
        for trace_id in expired:
            self._traces.pop(trace_id, None)


class LocalTraceSpanProcessor(SpanProcessor):
    def __init__(self, store: LocalTraceStore) -> None:
        self.store = store

    def on_start(self, span: Span, parent_context=None) -> None:
        self.store.upsert(span, running=True)

    def on_end(self, span: ReadableSpan) -> None:
        self.store.upsert(span, running=False)


TRACE_STORE = LocalTraceStore(
    max_traces=int(os.getenv("LOCAL_TRACE_MAX_TRACES", "80")),
    ttl_seconds=int(os.getenv("LOCAL_TRACE_TTL_SECONDS", "3600")),
)
_processor_registered = False


def register_local_trace_processor() -> bool:
    """Attach the local processor to the provider configured by AgentScope."""
    global _processor_registered
    if _processor_registered:
        return True
    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        return False
    provider.add_span_processor(LocalTraceSpanProcessor(TRACE_STORE))
    _processor_registered = True
    return True


@contextmanager
def request_trace_span(
    name: str,
    attributes: Mapping[str, str] | None = None,
) -> Iterator[str]:
    """Create a request root span and expose its hexadecimal trace id."""
    tracer = trace.get_tracer("plan-generator.frontend")
    with tracer.start_as_current_span(name, attributes=dict(attributes or {})) as span:
        context = span.get_span_context()
        yield _identifier(context.trace_id, 32) if context.is_valid else ""


def get_local_trace(trace_id: str) -> dict[str, Any] | None:
    return TRACE_STORE.get(trace_id)
