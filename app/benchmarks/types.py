"""Type definitions for benchmark evaluation."""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class RunQuestionResult:
    """Result from running a question through a text-to-SQL system."""

    predicted_sql: str
    latency_ms: int | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    model_name: str | None = None
    error: str | None = None
    trace: dict[str, Any] | None = None


@dataclass
class EvalCaseResult:
    """Result of evaluating a single benchmark case."""

    dataset: str
    index: int
    question_id: int | None = None
    db_id: str | None = None
    question: str = ""
    gold_sql: str = ""
    predicted_sql: str = ""
    exact_match: bool = False
    execution_match: bool | None = None
    analyzer_match: bool | None = None
    error: str | None = None
    execution_error: str | None = None
    gold_execution_results: list[dict] | None = None
    predicted_execution_results: list[dict] | None = None
    db_name: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    agent_trace: dict[str, Any] | None = None
    analyzer_output: dict[str, Any] | None = None
