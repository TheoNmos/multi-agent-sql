from __future__ import annotations

from typing import Any

from pydantic_ai.usage import RunUsage


def empty_usage_dict() -> dict[str, Any]:
    return {
        "requests": 0,
        "tool_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "details": {},
    }


def usage_to_dict(usage: RunUsage | None) -> dict[str, Any]:
    if usage is None:
        return empty_usage_dict()

    return {
        "requests": usage.requests,
        "tool_calls": usage.tool_calls,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "total_tokens": usage.total_tokens,
        "details": dict(usage.details),
    }


def merge_usage_dicts(*usage_dicts: dict[str, Any] | None) -> dict[str, Any]:
    merged = empty_usage_dict()

    for usage in usage_dicts:
        if not usage:
            continue

        merged["requests"] += int(usage.get("requests", 0) or 0)
        merged["tool_calls"] += int(usage.get("tool_calls", 0) or 0)
        merged["input_tokens"] += int(usage.get("input_tokens", 0) or 0)
        merged["output_tokens"] += int(usage.get("output_tokens", 0) or 0)

        details = usage.get("details") or {}
        if isinstance(details, dict):
            for key, value in details.items():
                merged["details"][key] = merged["details"].get(key, 0) + int(value or 0)

    merged["total_tokens"] = merged["input_tokens"] + merged["output_tokens"]
    return merged
