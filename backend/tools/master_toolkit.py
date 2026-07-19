"""Master-agent Toolkit assembly."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from agentscope.tool import Toolkit

from .config import MASTER_TOOL_GROUPS, get_enabled_master_tool_groups
from .context import (
    build_get_session_snapshot_tool,
    build_list_registered_skills_tool,
    build_retrieve_knowledge_tool,
)
from .cloud_query import (
    build_query_cloud_inventory_tool,
    build_query_cloud_metrics_tool,
    build_query_resource_group_products_tool,
)
from .document import build_get_generated_document_info_tool
from .generation import (
    build_generate_maintenance_plan_tool,
    build_prepare_plan_context_tool,
)
from .history import build_lookup_history_tool
from .impact_verification import build_analyze_impact_tool
from .planning import (
    build_check_missing_requirements_tool,
    build_update_requirements_tool,
)


@dataclass(frozen=True)
class MasterToolSpec:
    func: Callable[..., Any]
    func_name: str
    group_name: str | None = None


def _create_tool_groups(toolkit: Toolkit, enabled_groups: set[str]) -> None:
    for group_name, config in MASTER_TOOL_GROUPS.items():
        if group_name not in enabled_groups:
            continue
        toolkit.create_tool_group(
            group_name,
            description=config["description"],
            active=config["active"],
            notes=config["notes"],
        )


def _build_tool_specs(session: dict[str, Any], runtime: Any) -> list[MasterToolSpec]:
    return [
        MasterToolSpec(
            func=build_get_session_snapshot_tool(session),
            func_name="get_session_snapshot",
        ),
        MasterToolSpec(
            func=build_list_registered_skills_tool(runtime),
            group_name="context",
            func_name="list_registered_skills",
        ),
        MasterToolSpec(
            func=build_retrieve_knowledge_tool(),
            group_name="context",
            func_name="retrieve_knowledge",
        ),
        MasterToolSpec(
            func=build_update_requirements_tool(session),
            group_name="planning",
            func_name="update_requirements",
        ),
        MasterToolSpec(
            func=build_check_missing_requirements_tool(session),
            group_name="planning",
            func_name="check_missing_requirements",
        ),
        MasterToolSpec(
            func=build_prepare_plan_context_tool(session, runtime),
            group_name="generation",
            func_name="prepare_plan_context",
        ),
        MasterToolSpec(
            func=build_generate_maintenance_plan_tool(session, runtime),
            group_name="generation",
            func_name="generate_maintenance_plan",
        ),
        MasterToolSpec(
            func=build_get_generated_document_info_tool(session),
            group_name="document",
            func_name="get_generated_document_info",
        ),
        MasterToolSpec(
            func=build_lookup_history_tool(session),
            group_name="history",
            func_name="lookup_maintenance_history",
        ),
        MasterToolSpec(
            func=build_query_cloud_inventory_tool(),
            group_name="cloud_query",
            func_name="query_cloud_inventory",
        ),
        MasterToolSpec(
            func=build_query_cloud_metrics_tool(),
            group_name="cloud_query",
            func_name="query_cloud_metrics",
        ),
        MasterToolSpec(
            func=build_query_resource_group_products_tool(),
            group_name="cloud_query",
            func_name="query_resource_group_products",
        ),
        MasterToolSpec(
            func=build_analyze_impact_tool(session),
            group_name="cloud_query",
            func_name="analyze_maintenance_impact",
        ),
    ]


async def build_master_toolkit(
    session: dict[str, Any],
    runtime: Any,
) -> Toolkit:
    """Build a per-session toolkit with configured, stateful workflow tools."""
    toolkit = Toolkit()
    enabled_groups = get_enabled_master_tool_groups()
    _create_tool_groups(toolkit, enabled_groups)

    toolkit.register_tool_function(runtime.read_file)
    for spec in _build_tool_specs(session, runtime):
        if spec.group_name and spec.group_name not in enabled_groups:
            continue
        kwargs: dict[str, str] = {"func_name": spec.func_name}
        if spec.group_name:
            kwargs["group_name"] = spec.group_name
        toolkit.register_tool_function(spec.func, **kwargs)

    toolkit.register_tool_function(toolkit.reset_equipped_tools)
    runtime.register_skills(toolkit)
    return toolkit
