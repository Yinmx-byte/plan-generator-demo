"""Configure AgentScope native tracing before agents are constructed."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from urllib.request import urlopen

import agentscope
from dotenv import load_dotenv


BACKEND_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(BACKEND_ROOT / ".env", override=False)

_initialized = False
_status: dict[str, Any] = {
    "enabled": False,
    "initialized": False,
    "backend": "disabled",
}


def _is_enabled(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _public_url(value: str) -> str:
    """Drop credentials, query strings, and fragments from displayed URLs."""
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, host, parsed.path.rstrip("/"), "", ""))


def initialize_agentscope_observability() -> dict[str, Any]:
    """Initialize Studio or OTLP tracing once per backend process."""
    global _initialized, _status
    if _initialized:
        return dict(_status)

    enabled = _is_enabled(os.getenv("AGENTSCOPE_OBSERVABILITY_ENABLED", "false"))
    studio_url = os.getenv("AGENTSCOPE_STUDIO_URL", "").strip().rstrip("/")
    tracing_url = os.getenv("AGENTSCOPE_TRACING_URL", "").strip()
    project = os.getenv("AGENTSCOPE_PROJECT", "plan-generator").strip()
    run_name = os.getenv("AGENTSCOPE_RUN_NAME", "backend").strip()
    service_name = os.getenv("OTEL_SERVICE_NAME", "plan-generator-backend").strip()

    if not enabled:
        _status = {
            "enabled": False,
            "initialized": False,
            "backend": "disabled",
        }
        _initialized = True
        return dict(_status)

    if not studio_url and not tracing_url:
        _status = {
            "enabled": True,
            "initialized": False,
            "backend": "unconfigured",
            "error": "AGENTSCOPE_STUDIO_URL or AGENTSCOPE_TRACING_URL is required",
        }
        _initialized = True
        return dict(_status)

    os.environ.setdefault("OTEL_SERVICE_NAME", service_name)
    try:
        agentscope.init(
            project=project,
            name=run_name,
            studio_url=studio_url or None,
            tracing_url=tracing_url or None,
        )
    except Exception as exc:
        _status = {
            "enabled": True,
            "initialized": False,
            "backend": "studio" if studio_url else "otlp",
            "project": project,
            "run_name": run_name,
            "service_name": service_name,
            "error": f"{type(exc).__name__}: {exc}",
        }
    else:
        _status = {
            "enabled": True,
            "initialized": True,
            "backend": "studio" if studio_url else "otlp",
            "project": project,
            "run_name": run_name,
            "service_name": service_name,
            "studio_url": _public_url(studio_url) if studio_url else "",
            "tracing_url_configured": bool(tracing_url),
        }
    _initialized = True
    print(f"[AgentScope] Observability: {_status}")
    return dict(_status)


def get_observability_status(check_connection: bool = False) -> dict[str, Any]:
    """Return non-sensitive tracing configuration and optional Studio health."""
    status = dict(_status)
    status["agentscope_version"] = getattr(agentscope, "__version__", "unknown")
    studio_url = str(status.get("studio_url") or "")
    if check_connection and studio_url:
        try:
            with urlopen(studio_url, timeout=1.5) as response:
                status["studio_reachable"] = 200 <= response.status < 500
        except Exception as exc:
            status["studio_reachable"] = False
            status["connection_error"] = f"{type(exc).__name__}: {exc}"
    return status
