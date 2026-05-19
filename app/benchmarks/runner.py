"""Orchestrates benchmark evaluation runs."""

import asyncio
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.agents.llm_timeout import format_model_error
from app.config import db_settings

from .analyzer import analyze_benchmark_result
from .datasets import load_bird, load_livraria, select_items
from .metrics import exact_match, execution_match_async
from .run_question import run_question
from .types import EvalCaseResult, RunQuestionResult

logger = logging.getLogger(__name__)

# Default database name mapping
DB_NAME_MAP = {
    "bird": "passarinho",
    "livraria": "livraria",
}


def get_db_name(dataset: str, override: str | None = None) -> str:
    """Get database name for dataset, with optional override."""
    if override:
        return override
    return DB_NAME_MAP.get(dataset, dataset)


def extract_question_text(item: dict[str, Any], dataset: str) -> str:
    """Extract question text from dataset item."""
    if dataset == "livraria":
        return item.get("pergunta", "")
    elif dataset == "bird":
        return item.get("question", "")
    else:
        raise ValueError(f"Unknown dataset: {dataset}")


def extract_gold_sql(item: dict[str, Any], dataset: str) -> str:
    """Extract gold SQL from dataset item."""
    if dataset == "livraria":
        return item.get("sql", "")
    elif dataset == "bird":
        return item.get("SQL", "")
    else:
        raise ValueError(f"Unknown dataset: {dataset}")


def _result_to_dict(result: EvalCaseResult) -> dict[str, Any]:
    """Convert EvalCaseResult to dictionary for JSON serialization."""
    return {
        "dataset": result.dataset,
        "index": result.index,
        "question_id": result.question_id,
        "db_id": result.db_id,
        "question": result.question,
        "gold_sql": result.gold_sql,
        "predicted_sql": result.predicted_sql,
        "exact_match": result.exact_match,
        "execution_match": result.execution_match,
        "analyzer_match": result.analyzer_match,
        "error": result.error,
        "execution_error": result.execution_error,
        "gold_execution_results": result.gold_execution_results,
        "predicted_execution_results": result.predicted_execution_results,
        "db_name": result.db_name,
        "metadata": result.metadata,
        "agent_trace": result.agent_trace,
        "analyzer_output": result.analyzer_output,
    }


def _append_result_json(result: EvalCaseResult, output_path: Path) -> None:
    """Append a single result to a JSON array file."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    result_dict = _result_to_dict(result)

    # Read existing results or start with empty array
    existing_results = []
    if output_path.exists():
        try:
            with open(output_path, encoding="utf-8") as f:
                content = f.read()
                if content.strip():
                    existing_results = json.loads(content)
                    # Ensure it's a list
                    if not isinstance(existing_results, list):
                        existing_results = []
        except (json.JSONDecodeError, ValueError, OSError):
            # File exists but is invalid JSON or can't be read, start fresh
            existing_results = []

    # Append new result
    existing_results.append(result_dict)

    # Write back to file (atomic write: write to temp file then rename)
    temp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(existing_results, f, ensure_ascii=False, indent=2)
        # Atomic rename (works on Unix and Windows)
        temp_path.replace(output_path)
    except Exception:
        # If temp file write fails, try direct write as fallback
        try:
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(existing_results, f, ensure_ascii=False, indent=2)
        except Exception:
            # If all else fails, at least log the error
            logger.error(f"Failed to append result to {output_path}", exc_info=True)


async def _process_single_index(
    idx: int,
    item: dict[str, Any],
    dataset: str,
    final_db_name: str,
    final_server_dsn: str,
    metrics: list[str],
    timeout_s: int,
    use_gold_as_pred: bool,
    semaphore: asyncio.Semaphore,
    run_analyzer: bool = True,
    save_callback: Callable[[EvalCaseResult], None] | None = None,
) -> EvalCaseResult:
    """Process a single benchmark index with concurrency control."""
    async with semaphore:
        try:
            return await _process_single_index_body(
                idx=idx,
                item=item,
                dataset=dataset,
                final_db_name=final_db_name,
                final_server_dsn=final_server_dsn,
                metrics=metrics,
                timeout_s=timeout_s,
                use_gold_as_pred=use_gold_as_pred,
                run_analyzer=run_analyzer,
                save_callback=save_callback,
            )
        except Exception as e:
            logger.error(f"Index {idx}: unhandled benchmark error: {e}", exc_info=True)
            question = extract_question_text(item, dataset)
            gold_sql = extract_gold_sql(item, dataset)
            result = EvalCaseResult(
                dataset=dataset,
                index=idx,
                question_id=item.get("question_id"),
                db_id=item.get("db_id") if dataset == "bird" else None,
                question=question,
                gold_sql=gold_sql,
                predicted_sql="",
                exact_match=False,
                execution_match=None,
                analyzer_match=None,
                error=format_model_error(e),
                execution_error=None,
                gold_execution_results=None,
                predicted_execution_results=None,
                db_name=final_db_name,
                metadata={},
                agent_trace=None,
                analyzer_output=None,
            )
            if save_callback:
                save_callback(result)
            return result


async def _process_single_index_body(
    idx: int,
    item: dict[str, Any],
    dataset: str,
    final_db_name: str,
    final_server_dsn: str,
    metrics: list[str],
    timeout_s: int,
    use_gold_as_pred: bool,
    run_analyzer: bool,
    save_callback: Callable[[EvalCaseResult], None] | None,
) -> EvalCaseResult:
    question = extract_question_text(item, dataset)
    gold_sql = extract_gold_sql(item, dataset)
    # Get predicted SQL
    if use_gold_as_pred:
        run_result = RunQuestionResult(predicted_sql=gold_sql, trace=None)
    else:
        print(f"Running question: {question} on database {final_db_name}")
        run_result = await run_question(question, final_db_name)
        print(f"Run result: {run_result}")

    pred_sql = run_result.predicted_sql

    # Compute metrics
    compute_em = "em" in metrics
    compute_exa = "exa" in metrics

    em_result = False
    if compute_em:
        em_result = exact_match(gold_sql, pred_sql)

    exa_result: bool | None = None
    exa_error: str | None = None
    gold_execution_results: list[dict[str, Any]] | None = None
    predicted_execution_results: list[dict[str, Any]] | None = None
    if compute_exa:
        if not pred_sql.strip():
            exa_error = "Predicted SQL is empty, skipping execution"
            logger.warning(f"Index {idx}: Skipping execution - predicted SQL is empty")
        else:
            logger.debug(f"Index {idx}: Executing queries for execution accuracy check")
            (
                exa_result,
                exa_error,
                gold_execution_results,
                predicted_execution_results,
            ) = await execution_match_async(final_server_dsn, final_db_name, gold_sql, pred_sql, timeout_s)
    else:
        logger.debug(f"Index {idx}: Skipping execution - 'exa' metric not requested")

    # Run analyzer if enabled and neither EM nor RM is True
    analyzer_output = None
    analyzer_match: bool | None = None

    # If EM or RM is True, analyzer_match is True (rule-based match confirms correctness)
    if em_result or (exa_result is True):
        analyzer_match = True
        analyzer_output = {
            "analyzer_match": True,
            "approved": True,
            "failure_step": "none",
            "failure_reason": "Result is correct - rule-based match (EM or RM) confirms correctness",
            "query_issues": "No issues found - query matches expected results",
            "root_cause": "Result is correct - validated by exact match or execution match",
            "suggestions": [],
        }
        logger.info(f"Index {idx}: Skipping analyzer - rule-based match (EM or RM) confirms correctness")
    elif run_analyzer:
        # Run analyzer only if enabled and neither EM nor RM matched
        logger.info(f"Index {idx}: Running analyzer to check business impact (AM)")
        try:
            # Prepare result data for analyzer
            result_data = {
                "dataset": dataset,
                "index": idx,
                "question_id": item.get("question_id"),
                "db_id": item.get("db_id") if dataset == "bird" else None,
                "question": question,
                "gold_sql": gold_sql,
                "predicted_sql": pred_sql,
                "exact_match": em_result,
                "execution_match": exa_result,
                "error": run_result.error,
                "execution_error": exa_error,
                "gold_execution_results": gold_execution_results,
                "predicted_execution_results": predicted_execution_results,
                "agent_trace": run_result.trace,
            }
            analyzer_result = await analyze_benchmark_result(result_data)
            # Convert Pydantic model to dict for storage
            analyzer_output = analyzer_result.model_dump()
            analyzer_match = analyzer_result.analyzer_match
        except Exception as e:
            logger.error(f"Index {idx}: Error running analyzer: {e}", exc_info=True)
            analyzer_failure = format_model_error(e, step="Analyzer")
            analyzer_output = {
                "analyzer_match": False,
                "approved": False,
                "failure_step": "analyzer",
                "failure_reason": analyzer_failure,
                "query_issues": "Unable to analyze due to analyzer error",
                "root_cause": "Analyzer agent encountered an error",
                "suggestions": ["Fix analyzer agent error handling"],
            }
            analyzer_match = False
    else:
        # Analyzer disabled and no rule-based match
        analyzer_match = None
        logger.info(f"Index {idx}: Analyzer disabled - no AM check performed")

    # Build result
    result = EvalCaseResult(
        dataset=dataset,
        index=idx,
        question_id=item.get("question_id"),
        db_id=item.get("db_id") if dataset == "bird" else None,
        question=question,
        gold_sql=gold_sql,
        predicted_sql=pred_sql,
        exact_match=em_result,
        execution_match=exa_result,
        analyzer_match=analyzer_match,
        error=run_result.error,
        execution_error=exa_error,
        gold_execution_results=gold_execution_results,
        predicted_execution_results=predicted_execution_results,
        db_name=final_db_name,
        metadata={
            "latency_ms": run_result.latency_ms,
            "tokens_in": run_result.tokens_in,
            "tokens_out": run_result.tokens_out,
            "model_name": run_result.model_name,
        },
        agent_trace=run_result.trace,
        analyzer_output=analyzer_output,
    )

    # Save result incrementally if callback provided
    if save_callback:
        save_callback(result)

    return result


async def run_benchmark(
    dataset: str,
    indices: list[int],
    metrics: list[str],
    server_dsn: str | None = None,
    db_name: str | None = None,
    timeout_s: int = 30,
    use_gold_as_pred: bool = False,
    output_path: str | None = None,
    max_concurrent: int = 5,
    run_analyzer: bool = True,
    incremental_save: bool = False,
) -> list[EvalCaseResult]:
    """
    Run benchmark evaluation on a subset of items with concurrent execution.

    Args:
        dataset: Dataset name ('bird' or 'livraria')
        indices: 1-based indices of items to evaluate
        metrics: List of metrics to compute ('em', 'exa') - always computed
        server_dsn: Database server DSN (defaults to db_settings.db_url)
        db_name: Database name (defaults to mapping)
        timeout_s: Timeout for SQL execution in seconds
        use_gold_as_pred: If True, use gold SQL as predicted SQL (for testing harness)
        output_path: Path to write JSONL results (optional)
        max_concurrent: Maximum number of concurrent tasks (default: 5)
        run_analyzer: If True, run analyzer agent when EM and RM are both False

    Returns:
        List of EvalCaseResult for each evaluated item
    """
    # Load dataset
    if dataset == "bird":
        all_items = load_bird()
    elif dataset == "livraria":
        all_items = load_livraria()
    else:
        raise ValueError(f"Unknown dataset: {dataset}")

    # Select items
    selected_items = select_items(all_items, indices)

    # Get database name
    final_db_name = get_db_name(dataset, db_name)
    final_server_dsn = server_dsn or db_settings.db_url

    # Create semaphore to limit concurrency
    semaphore = asyncio.Semaphore(max_concurrent)

    # Set up incremental save callback if enabled
    save_callback: Callable[[EvalCaseResult], None] | None = None
    if incremental_save and output_path:
        output_file = Path(output_path)
        # Use JSON format (array) for incremental saves
        if output_file.suffix != ".json":
            output_file = output_file.with_suffix(".json")
        # Initialize file with empty array if it doesn't exist
        if not output_file.exists():
            output_file.parent.mkdir(parents=True, exist_ok=True)
            with open(output_file, "w", encoding="utf-8") as f:
                json.dump([], f)
        # Append to existing (allows resuming/cross-batch appends)
        save_callback = lambda r: _append_result_json(r, output_file)

    # Create tasks for all indices
    tasks = [
        _process_single_index(
            idx=idx,
            item=item,
            dataset=dataset,
            final_db_name=final_db_name,
            final_server_dsn=final_server_dsn,
            metrics=metrics,
            timeout_s=timeout_s,
            use_gold_as_pred=use_gold_as_pred,
            semaphore=semaphore,
            run_analyzer=run_analyzer,
            save_callback=save_callback,
        )
        for idx, item in zip(indices, selected_items, strict=True)
    ]

    # Run all tasks concurrently; return_exceptions so one hung task cannot block the batch.
    raw_results = await asyncio.gather(*tasks, return_exceptions=True)
    results: list[EvalCaseResult] = []
    for task_idx, raw in enumerate(raw_results):
        if isinstance(raw, EvalCaseResult):
            results.append(raw)
            continue
        index = indices[task_idx]
        item = selected_items[task_idx]
        exc = raw
        logger.error(f"Index {index}: task raised unexpectedly: {exc}", exc_info=exc)
        question = extract_question_text(item, dataset)
        gold_sql = extract_gold_sql(item, dataset)
        fallback = EvalCaseResult(
            dataset=dataset,
            index=index,
            question_id=item.get("question_id"),
            db_id=item.get("db_id") if dataset == "bird" else None,
            question=question,
            gold_sql=gold_sql,
            predicted_sql="",
            exact_match=False,
            execution_match=None,
            analyzer_match=None,
            error=format_model_error(exc),
            execution_error=None,
            gold_execution_results=None,
            predicted_execution_results=None,
            db_name=final_db_name,
            metadata={},
            agent_trace=None,
            analyzer_output=None,
        )
        if save_callback:
            save_callback(fallback)
        results.append(fallback)

    # Sort results by index to maintain order
    results = sorted(results, key=lambda r: r.index)

    # Write JSON array output if path provided and not using incremental save
    if output_path and not incremental_save:
        output_file = Path(output_path)
        output_file.parent.mkdir(parents=True, exist_ok=True)
        with open(output_file, "w", encoding="utf-8") as f:
            result_list = [_result_to_dict(result) for result in results]
            json.dump(result_list, f, ensure_ascii=False, indent=2)

    return results


def print_summary(results: list[EvalCaseResult], metrics: list[str]) -> None:
    """Print summary statistics for benchmark results."""
    total = len(results)

    if "em" in metrics:
        em_correct = sum(1 for r in results if r.exact_match)
        em_rate = (em_correct / total * 100) if total > 0 else 0.0
        print(f"\nExact Match (EM): {em_correct}/{total} ({em_rate:.1f}%)")

    if "exa" in metrics:
        exa_results = [r.execution_match for r in results if r.execution_match is not None]
        if exa_results:
            exa_correct = sum(1 for r in exa_results if r is True)
            exa_total = len(exa_results)
            exa_rate = (exa_correct / exa_total * 100) if exa_total > 0 else 0.0
            print(f"Result Match (RM/Execution Accuracy): {exa_correct}/{exa_total} ({exa_rate:.1f}%)")
            exa_errors = sum(1 for r in results if r.execution_error is not None)
            if exa_errors > 0:
                print(f"  (RM errors: {exa_errors})")
        else:
            print("Result Match (RM/Execution Accuracy): No valid results")

    # Analyzer Match (AM) - always computed unless EM or RM is True
    am_results = [r.analyzer_match for r in results if r.analyzer_match is not None]
    if am_results:
        am_correct = sum(1 for r in am_results if r is True)
        am_total = len(am_results)
        am_rate = (am_correct / am_total * 100) if am_total > 0 else 0.0
        print(f"Analyzer Match (AM): {am_correct}/{am_total} ({am_rate:.1f}%)")
        print(f"  (AM checks business impact equivalence)")

    errors = sum(1 for r in results if r.error is not None)
    if errors > 0:
        print(f"\nSystem errors: {errors}/{total}")
