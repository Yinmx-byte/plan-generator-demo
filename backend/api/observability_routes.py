"""Read-only endpoints for AgentScope tracing status."""

from fastapi import APIRouter

from observability import get_observability_status


router = APIRouter(prefix="/api/observability", tags=["observability"])


@router.get("/status")
def observability_status() -> dict:
    """Report whether AgentScope Studio tracing is configured and reachable."""
    return get_observability_status(check_connection=True)
