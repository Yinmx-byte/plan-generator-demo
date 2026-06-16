"""Lightweight tool-group configuration for the master agent."""

from __future__ import annotations

import os
from typing import Any


MASTER_TOOL_GROUPS: dict[str, dict[str, Any]] = {
    "context": {
        "description": "Skill discovery and RAG retrieval.",
        "active": False,
        "notes": (
            "Use for inspecting available Skills and retrieving knowledge evidence. "
            "Do not hard-code product-to-Skill routing; rely on AgentScope Skill selection."
        ),
    },
    "planning": {
        "description": "Requirement extraction and missing-field checks.",
        "active": False,
        "notes": "Use for maintenance-plan requirement extraction and follow-up questions.",
    },
    "generation": {
        "description": "Maintenance-plan evidence preparation and DOCX generation.",
        "active": False,
        "notes": "Use only after required maintenance fields are complete.",
    },
    "document": {
        "description": "Generated document inspection helpers.",
        "active": False,
        "notes": "Use after a document has been generated or when the user asks about the generated file.",
    },
}

DEFAULT_MASTER_TOOL_GROUPS = tuple(MASTER_TOOL_GROUPS.keys())


def get_enabled_master_tool_groups() -> set[str]:
    """Return enabled master-agent tool groups."""
    raw = os.getenv("MASTER_AGENT_TOOL_GROUPS", "all").strip()
    if not raw or raw.lower() == "all":
        return set(DEFAULT_MASTER_TOOL_GROUPS)
    if raw.lower() in {"none", "off", "disabled"}:
        return set()
    requested = {item.strip() for item in raw.split(",") if item.strip()}
    unknown = requested - set(MASTER_TOOL_GROUPS)
    if unknown:
        raise ValueError(f"Unknown MASTER_AGENT_TOOL_GROUPS values: {', '.join(sorted(unknown))}")
    return requested

