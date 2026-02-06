"""TOON (Token-Oriented Object Notation) encoder utilities for LLM prompts.

TOON is a compact, human-readable format designed for LLM communication.
This module provides encode-only functionality to convert Python data structures
to TOON format for embedding in LLM prompts.
"""

from __future__ import annotations

from typing import Any


def _is_primitive(value: Any) -> bool:
    """Check if value is a primitive type (str, int, float, bool, None)."""
    return isinstance(value, (str, int, float, bool, type(None)))


def _escape_value(value: Any) -> str:
    """Escape a TOON value (handles strings, numbers, booleans, None)."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        # Escape quotes and newlines in strings
        escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        # Quote strings that contain special characters or spaces
        if any(c in escaped for c in [",", ":", "{", "}", "[", "]", " ", "\t"]):
            return f'"{escaped}"'
        return escaped
    # Fallback: convert to string
    return str(value)


def _is_uniform_list(items: list[Any]) -> bool:
    """Check if list contains uniform objects (all dicts with same keys)."""
    if not items or not all(isinstance(item, dict) for item in items):
        return False
    if len(items) == 1:
        return True
    # Check if all dicts have the same keys
    first_keys = set(items[0].keys())
    return all(set(item.keys()) == first_keys for item in items[1:])


def _encode_value(value: Any, indent: int = 0) -> str:
    """Encode a single value to TOON format."""
    indent_str = "  " * indent

    if _is_primitive(value):
        return _escape_value(value)

    if isinstance(value, dict):
        if not value:
            return "{}"
        lines = []
        for key, val in value.items():
            key_str = _escape_value(key) if not isinstance(key, str) or any(c in key for c in [":", " ", "\t"]) else key
            val_str = _encode_value(val, indent + 1)
            # If value is multiline (object or array), indent it
            if "\n" in val_str:
                lines.append(f"{indent_str}{key_str}:\n{val_str}")
            else:
                lines.append(f"{indent_str}{key_str}: {val_str}")
        return "\n".join(lines)

    if isinstance(value, list):
        if not value:
            return "[]"
        # Check if uniform list of objects
        if _is_uniform_list(value):
            # Get field names from first item
            first_item = value[0]
            field_names = list(first_item.keys())
            field_header = ",".join(field_names)
            lines = [f"{indent_str}[{len(value)}]{{{field_header}}}:"]
            # Encode each row
            for item in value:
                row_values = [_escape_value(item.get(field, None)) for field in field_names]
                lines.append(f"{indent_str}  {','.join(row_values)}")
            return "\n".join(lines)
        # Non-uniform list: encode each item
        lines = [f"{indent_str}[{len(value)}]:"]
        for item in value:
            item_str = _encode_value(item, indent + 1)
            if "\n" in item_str:
                lines.append(f"{indent_str}  {item_str}")
            else:
                # Single line item
                item_indent = "  " * (indent + 1)
                lines.append(f"{item_indent}{item_str}")
        return "\n".join(lines)

    # Fallback: convert to string
    return _escape_value(value)


def to_toon(data: Any, root_name: str = "data") -> str:
    """
    Encode Python data structure to TOON format.

    Args:
        data: Python data structure (dict, list, primitive)
        root_name: Name for root object (used if data is a dict)

    Returns:
        TOON-formatted string
    """
    if isinstance(data, dict):
        # For dicts, use root_name as the object name
        result = _encode_value(data, indent=0)
        return result
    elif isinstance(data, list):
        # For lists, use root_name as the array name
        return _encode_value(data, indent=0)
    else:
        # For primitives, just encode the value
        return _escape_value(data)


def to_toon_block(data: Any, root_name: str = "data") -> str:
    """
    Encode Python data structure to TOON format wrapped in a fenced code block.

    Args:
        data: Python data structure (dict, list, primitive)
        root_name: Name for root object (used if data is a dict)

    Returns:
        TOON-formatted string wrapped in ```toon code fence
    """
    toon_content = to_toon(data, root_name)
    return f"```toon\n{toon_content}\n```"
