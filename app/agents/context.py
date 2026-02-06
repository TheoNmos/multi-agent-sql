"""Shared context and state models for the new multi-agent system."""

from __future__ import annotations

from typing import Any

import asyncpg
from pydantic import BaseModel, ConfigDict, Field


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


# mapperOutput is now just a string - a natural language summary of relevant schema and context
mapperOutput = str


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

    # Database connection
    database_connection: asyncpg.Connection | None = None

    # Scratchpad for coordination
    scratch: dict[str, Any] = Field(default_factory=dict)

    # Pre-validated syntax results (set by compositor)
    syntax_valid: bool | None = None
    syntax_error: str | None = None

    # Trace for debugging and analysis
    trace: Trace = Field(default_factory=Trace)
