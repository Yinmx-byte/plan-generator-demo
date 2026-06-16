"""Generated document inspection tools for the master agent."""

from __future__ import annotations

from typing import Any, Callable

from agentscope.tool import ToolResponse

from services.plan_generation import docx_to_markdown, get_generated_file

from .responses import json_tool_response


def build_get_generated_document_info_tool(
    session: dict[str, Any],
) -> Callable[[bool], ToolResponse]:
    def get_generated_document_info(include_preview: bool = False) -> ToolResponse:
        """Return metadata and optional text preview for the latest generated DOCX.

        Args:
            include_preview: Whether to include a Markdown text preview of the
                latest generated document.
        """
        generated = session.get("generated")
        if not generated:
            return json_tool_response(
                {"status": "not_found", "message": "当前会话还没有生成文档。"}
            )
        path = get_generated_file(generated.get("file_id"))
        payload: dict[str, Any] = {
            "status": "ok" if path else "expired",
            "generated": generated,
            "exists": bool(path),
        }
        if path:
            payload["path"] = str(path)
            payload["size_bytes"] = path.stat().st_size
            if include_preview:
                payload["preview_markdown"] = docx_to_markdown(path.read_bytes())[:8000]
        return json_tool_response(payload)

    return get_generated_document_info

