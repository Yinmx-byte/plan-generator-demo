"""Read-only endpoints for AgentScope tracing status."""

from fastapi import APIRouter, HTTPException

from observability import get_local_trace, get_observability_status


router = APIRouter(prefix="/api/observability", tags=["observability"])


@router.get("/status")
def observability_status() -> dict:
    """Report whether AgentScope Studio tracing is configured and reachable."""
    return get_observability_status(check_connection=True)


@router.get("/traces/{trace_id}")
def observability_trace(trace_id: str) -> dict:
    """Return one recent sanitized trace for the business frontend."""
    normalized = trace_id.strip().lower()
    if len(normalized) != 32 or any(char not in "0123456789abcdef" for char in normalized):
        raise HTTPException(status_code=400, detail="Trace ID 格式无效")
    trace_data = get_local_trace(normalized)
    if not trace_data:
        raise HTTPException(status_code=404, detail="Trace 不存在、尚未完成或已经过期")
    return trace_data
