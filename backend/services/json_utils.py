"""JSON and AgentScope response parsing helpers."""

import json
import re
from typing import Any

from agentscope.tool import ToolResponse

def extract_json(text: Any) -> dict:
    """从 LLM 返回的文本中提取 JSON。"""
    if isinstance(text, dict) and "document" in text:
        return text
    if not isinstance(text, str):
        text = get_response_text(text)
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"):
            text = "\n".join(lines[1:])
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        if start < 0:
            raise

        depth = 0
        in_string = False
        escape = False
        for idx in range(start, len(text)):
            char = text[idx]
            if escape:
                escape = False
                continue
            if char == "\\":
                escape = True
                continue
            if char == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return json.loads(text[start : idx + 1])
        raise



def get_response_text(response) -> str:
    """Extract final answer text from AgentScope 1.x ChatResponse."""
    try:
        direct_text = response.get_text_content()
    except Exception:
        direct_text = None
    if isinstance(direct_text, str) and direct_text.strip():
        return direct_text.strip()

    if isinstance(response, dict) and isinstance(response.get("content"), list):
        final_blocks = [
            str(block.get("text", "")).strip()
            for block in response["content"]
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        final_text = "\n".join(text for text in final_blocks if text)
        if final_text:
            return final_text

    def collect(value: Any, depth: int = 0) -> list[str]:
        if value is None or depth > 8:
            return []
        if isinstance(value, str):
            return [value]
        if isinstance(value, (int, float, bool)):
            return []
        if isinstance(value, dict):
            if value.get("type") == "thinking":
                return []
            fragments: list[str] = []
            priority_keys = (
                "text",
                "content",
                "arguments",
                "input",
                "output",
                "message",
                "value",
                "tool_calls",
                "choices",
            )
            for key in priority_keys:
                if key in value:
                    fragments.extend(collect(value[key], depth + 1))
            for key, item in value.items():
                if key not in priority_keys and key not in {"type", "id", "role", "name"}:
                    fragments.extend(collect(item, depth + 1))
            if "document" in value:
                fragments.insert(0, json.dumps(value, ensure_ascii=False))
            return fragments
        if isinstance(value, (list, tuple)):
            fragments = []
            for item in value:
                fragments.extend(collect(item, depth + 1))
            return fragments

        fragments = []
        for method in ("get_text_content", "model_dump", "dict"):
            try:
                func = getattr(value, method)
            except (AttributeError, KeyError):
                continue
            if callable(func):
                try:
                    result = func()
                except TypeError:
                    continue
                fragments.extend(collect(result, depth + 1))

        for attr in ("text", "content", "message", "output", "tool_calls", "metadata"):
            try:
                attr_value = getattr(value, attr)
            except (AttributeError, KeyError):
                continue
            fragments.extend(collect(attr_value, depth + 1))
        return fragments

    seen = set()
    ordered = []
    for fragment in collect(response):
        cleaned = fragment.strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            ordered.append(cleaned)
    return "\n".join(ordered)


