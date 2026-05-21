from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.db.adapter import DatabaseAdapter


class QueryPlan(BaseModel):
    """Query execution plan details."""

    total_cost: str | float = "N/A"
    rows_estimate: str | int = "N/A"
    actual_rows: int | None = None


class ValidationContext(BaseModel):
    """Pre-validated SQL validation results from orchestrator."""

    sqlglot_valid: bool | None = None
    sqlglot_error: str | None = None
    db_syntax_valid: bool | None = None
    db_syntax_error: str | None = None
    query_plan: QueryPlan | None = None


class RuntimeData(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    """Unified runtime state shared across all specialist agents.

    This model intentionally avoids schema-specific knowledge. Orchestrator will
    stitch agent outputs together using these fields.
    """

    # User input and context
    user_message: str | None = None

    # Planejador outputs
    short_plan: str | None = None  # Short NL plan of what to do
    slots: dict[str, str] = Field(default_factory=dict)  # slot_name -> value (schema-agnostic)

    # Mapeador outputs
    slot_to_schema: dict[str, str] = Field(
        default_factory=dict
    )  # slot_name -> qualified schema field (e.g., table.column)
    logical_skeleton: str | None = None  # schema-aligned logical skeleton

    # Schema catalog (cached)
    schema_catalog: dict[str, Any] | None = None  # Cached schema catalog data
    fk_graph: list[dict[str, str]] | None = None  # Foreign key relationships

    # Joiner outputs
    join_clauses: list[str] | None = None  # JOIN ON clauses
    tables: list[str] | None = None  # Tables involved in query

    # Compositor outputs
    sql_query: str | None = None

    # Validador outputs
    validator_feedback: str | None = None
    is_valid: bool | None = None
    is_best_query: bool | None = None

    # Coordination
    attempt_index: int = 0
    max_corrections: int = 3

    previous_query: str = ""
    all_generated_queries: list[str] = Field(default_factory=list)
    # Database connection (adapter)
    database_connection: DatabaseAdapter | None = None

    # Scratchpad for future tools or orchestrator
    scratch: dict[str, Any] = Field(default_factory=dict)

    # Pre-validated context (set by orchestrator)
    validation_context: ValidationContext | None = None
