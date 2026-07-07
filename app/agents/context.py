"""Shared context and state models for the new multi-agent system."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.db.adapter import DatabaseAdapter


class SupervisorOutput(BaseModel):
    """Output from the Supervisor agent when the pipeline is complete."""

    status: Literal["success", "reject", "error"] = Field(
        description="Pipeline outcome: success (valid SQL), reject (invalid after retries), error (failure)"
    )
    message: str = Field(description="Summary message or feedback")
    final_sql: str | None = Field(default=None, description="The final SQL query if status is success")


class InterpreterOutput(BaseModel):
    """Output from Agent 1: Query Interpreter."""

    clarified_question: str = Field(description="Rewritten and clarified version of the user question")
    sub_questions: list[str] = Field(
        default_factory=list, description="List of sub-questions if the query is complex (empty if simple)"
    )
    explicit_intent: str = Field(description="Explicit statement of what the user wants to find")
    ambiguities_resolved: list[str] = Field(
        default_factory=list, description="List of ambiguities that were resolved during clarification"
    )
    user_filter_literals: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Explicit filter values the user stated in the question (e.g. {'country': 'SVK', 'segment': 'Premium'}). "
            "These override mapper sample_values when generating SQL."
        ),
    )
    aggregation_granularity: Literal["row_level", "entity_level", "unspecified"] = Field(
        default="unspecified",
        description=(
            "Whether percentages/counts are over individual rows (e.g. transactions) "
            "or distinct entities (e.g. customers)."
        ),
    )


MapperColumnRole = Literal["select", "filter", "join", "aggregate", "order", "group", "context"]


class MappedColumn(BaseModel):
    """Column selected by the mapper and the role it should play in SQL generation."""

    table_name: str = Field(description="Table that owns the column")
    column_name: str = Field(description="Column name exactly as it appears in the database")
    role: MapperColumnRole = Field(description="How this column should be used by the generator")
    reason: str = Field(description="Why this column is relevant to the clarified question")
    data_type: str | None = Field(default=None, description="Column type when known")
    sample_values: list[str] = Field(
        default_factory=list, description="Known or sampled values useful for semantic filtering"
    )
    value_format: str | None = Field(
        default=None, description="Observed format, such as YYYYMM, DATE, enum code, or free text"
    )


class MappedTable(BaseModel):
    """Table selected by the mapper with the evidence that supports using it."""

    table_name: str = Field(description="Table name exactly as it appears in the database")
    reason: str = Field(description="Why this table was selected over alternatives")
    priority: Literal["primary", "secondary", "fallback"] = Field(
        default="primary", description="How important this table is for answering the question"
    )
    relevant_columns: list[str] = Field(
        default_factory=list, description="Column names from this table that matter for the query"
    )
    sample_row: dict[str, Any] | None = Field(
        default=None, description="Small sample row or sample fragments already observed"
    )


class MappedJoin(BaseModel):
    """Join relationship the generator is allowed to use."""

    left_table: str = Field(description="Left/source table")
    left_column: str = Field(description="Join column on the left/source table")
    right_table: str = Field(description="Right/destination table")
    right_column: str = Field(description="Join column on the right/destination table")
    join_type: Literal["INNER", "LEFT", "RIGHT", "FULL"] = Field(
        default="INNER", description="Suggested SQL join type"
    )
    reason: str = Field(description="Why this join is needed")
    cardinality_warning: str | None = Field(
        default=None, description="Warning when this join can change the base entity count"
    )


class MapperOutput(BaseModel):
    """Structured schema mapping passed from the mapper to the SQL generator."""

    selected_tables: list[MappedTable] = Field(
        default_factory=list, description="Tables selected as relevant to the question"
    )
    columns: list[MappedColumn] = Field(
        default_factory=list, description="Relevant columns and their intended SQL roles"
    )
    joins: list[MappedJoin] = Field(
        default_factory=list, description="Join relationships that are required or allowed"
    )
    target_columns: list[str] = Field(
        default_factory=list, description="Fully qualified columns or expressions expected in SELECT"
    )
    filters: list[str] = Field(
        default_factory=list, description="Expected filters or predicates, including confirmed values"
    )
    required_constraints: list[str] = Field(
        default_factory=list,
        description="Intent-level constraints that must appear in the final SQL or be explicitly explained",
    )
    value_notes: list[str] = Field(
        default_factory=list, description="Value encodings, exact matches, date formats, and semantic notes"
    )
    cardinality_notes: list[str] = Field(
        default_factory=list, description="Warnings about joins, duplicate rows, or single-table alternatives"
    )
    validation_notes: list[str] = Field(
        default_factory=list, description="Checks performed and remaining uncertainties"
    )
    confidence: float = Field(default=0.0, description="Mapper confidence from 0.0 to 1.0")


# Backwards-compatible alias used by older imports.
mapperOutput = MapperOutput


class GeneratorOutput(BaseModel):
    """Output from Agent 3: SQL Generator."""

    reasoning_steps: list[str] = Field(
        default_factory=list, description="Step-by-step reasoning for how the query was constructed"
    )
    sub_queries: list[dict[str, str]] = Field(
        default_factory=list,
        description="If query was decomposed, list of sub-queries with descriptions",
    )
    confidence: float = Field(default=0.0, description="Confidence score for the generated query (0.0-1.0)")
    sql_query: str = Field(description="The generated SQL query")


class ValidatorOutput(BaseModel):
    """Output from Agent 4: Validator & Refiner."""

    is_valid: bool = Field(description="Whether the SQL query is syntactically and semantically valid")
    is_optimal: bool = Field(description="Whether the query is optimal in terms of efficiency and best practices")
    syntax_errors: list[str] = Field(default_factory=list, description="List of syntax errors found (empty if valid)")
    semantic_issues: list[str] = Field(
        default_factory=list, description="List of semantic issues found (empty if none)"
    )
    efficiency_score: float = Field(default=0.0, description="Efficiency score based on query plan analysis (0.0-1.0)")
    efficiency_issues: list[str] = Field(
        default_factory=list, description="List of efficiency issues found (empty if optimal)"
    )
    refinement_feedback: str = Field(default="", description="Detailed feedback for improving the query")


class StepInfo(BaseModel):
    """Information about a pipeline step execution."""

    name: str = Field(description="Step name: interpreter, mapper, generator, validator")
    timing_ms: int = Field(description="Step execution time in milliseconds")
    input_summary: dict[str, Any] = Field(default_factory=dict, description="Summary of step inputs")
    output_summary: dict[str, Any] | None = Field(default=None, description="Summary of step outputs")
    error: str | None = Field(default=None, description="Error message if step failed")


class ToolCall(BaseModel):
    """Information about a tool call during pipeline execution."""

    agent: str = Field(description="Agent that made the tool call")
    tool: str = Field(description="Tool name that was called")
    args_redacted: Any = Field(description="Tool arguments (may be redacted/limited)")
    result_preview: str | None = Field(default=None, description="Preview of tool result")
    timing_ms: int = Field(description="Tool execution time in milliseconds")
    error: str | None = Field(default=None, description="Error message if tool call failed")


class GeneratorTrace(BaseModel):
    """Generator-specific trace information."""

    sql_query: str = Field(description="Generated SQL query")
    reasoning_steps: list[str] = Field(default_factory=list, description="Reasoning steps")
    confidence: float = Field(description="Confidence score")


class mapperTrace(BaseModel):
    """mapper-specific trace information."""

    all_tables_count: int = Field(default=0, description="Total number of tables in database")
    selected_tables: list[str] = Field(default_factory=list, description="Tables selected by mapper")


class Trace(BaseModel):
    """Complete trace of pipeline execution."""

    pipeline: str = Field(default="new", description="Pipeline identifier")
    db_name: str | None = Field(default=None, description="Database name")
    start_ts: str | None = Field(default=None, description="Pipeline start timestamp (ISO format)")
    end_ts: str | None = Field(default=None, description="Pipeline end timestamp (ISO format)")
    latency_ms: int | None = Field(default=None, description="Total pipeline latency in milliseconds")
    steps: list[StepInfo] = Field(default_factory=list, description="Pipeline step executions")
    tools: list[ToolCall] = Field(default_factory=list, description="Tool calls made during execution")
    generator: GeneratorTrace | None = Field(default=None, description="Generator trace information")
    mapper: mapperTrace = Field(default_factory=mapperTrace, description="mapper trace information")


class PipelineResult(BaseModel):
    """Complete result from the multi-agent pipeline."""

    status: str = Field(description="Pipeline status: GENERATED, ERROR, REJECTED")
    sql: str | None = Field(default=None, description="Generated SQL query")
    error: str | None = Field(default=None, description="Error message if status is ERROR")
    plan: str | None = Field(default=None, description="Explicit intent/plan from interpreter")
    feedback: str | None = Field(default=None, description="Feedback or validation message")
    attempts: int = Field(default=1, description="Number of attempts made")
    all_queries: list[str] = Field(default_factory=list, description="All SQL queries generated during attempts")

    # Full agent outputs
    interpreter_output: InterpreterOutput | None = None
    mapper_output: mapperOutput | None = None
    generator_output: GeneratorOutput | None = None
    validator_output: ValidatorOutput | None = None

    # Trace information
    trace: Trace = Field(default_factory=Trace, description="Full trace of pipeline execution")


class AgentState(BaseModel):
    """Shared state/memory for all agents in the pipeline."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # User identity for per-user prompts and Redis scoping
    user_id: str | None = None

    # Session tracking
    session_id: str | None = None

    # User input
    raw_question: str
    clarified_question: str | None = None

    # Agent outputs
    interpreter_output: InterpreterOutput | None = None
    mapper_output: mapperOutput | None = None
    generator_output: GeneratorOutput | None = None
    validator_output: ValidatorOutput | None = None

    # Query history
    current_sql: str | None = None
    sql_history: list[str] = Field(default_factory=list)
    best_sql: str | None = None
    best_validator_output: ValidatorOutput | None = None

    # Iteration state
    attempt_count: int = 0
    max_attempts: int = 3

    # Database connection (adapter)
    database_connection: DatabaseAdapter | None = None

    # SQL dialect for prompts and dialect-aware behavior
    sql_dialect: Literal["postgres", "mysql"] = "postgres"

    # Scratchpad for coordination
    scratch: dict[str, Any] = Field(default_factory=dict)

    # Custom prompt overrides (agent_id -> prompt template), loaded from Redis
    custom_prompts: dict[str, str] = Field(default_factory=dict)

    # Supervisor tips per agent (agent_id -> tip text); set by supervisor before calling workers
    supervisor_tips: dict[str, str] = Field(default_factory=dict)

    # Pre-validated syntax results (set by supervisor)
    syntax_valid: bool | None = None
    syntax_error: str | None = None

    # Trace for debugging and analysis
    trace: Trace = Field(default_factory=Trace)
