"""Prompt templates and defaults for customizable agent prompts."""

from app.prompts.defaults import (
    AGENT_IDS,
    AGENT_PLACEHOLDERS,
    DEFAULT_GENERATOR_PROMPT,
    DEFAULT_INTERPRETER_PROMPT,
    DEFAULT_MAPPER_PROMPT,
    DEFAULT_SUPERVISOR_PROMPT,
    DEFAULT_VALIDATOR_PROMPT,
    MYSQL_DIALECT_NOTES,
    POSTGRES_DIALECT_NOTES,
    dialect_label,
    dialect_notes,
    format_supervisor_tips,
    get_default_prompt,
    render_prompt,
)

__all__ = [
    "AGENT_IDS",
    "AGENT_PLACEHOLDERS",
    "DEFAULT_INTERPRETER_PROMPT",
    "DEFAULT_MAPPER_PROMPT",
    "DEFAULT_GENERATOR_PROMPT",
    "DEFAULT_VALIDATOR_PROMPT",
    "DEFAULT_SUPERVISOR_PROMPT",
    "MYSQL_DIALECT_NOTES",
    "POSTGRES_DIALECT_NOTES",
    "dialect_label",
    "dialect_notes",
    "format_supervisor_tips",
    "get_default_prompt",
    "render_prompt",
]
