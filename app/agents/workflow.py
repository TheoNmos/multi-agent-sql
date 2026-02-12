"""Compositor (Supervisor) - Orchestrates the multi-agent text-to-SQL pipeline."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import logfire

from app.agents.context import AgentState, GeneratorTrace, PipelineResult, StepInfo, Trace, mapperTrace
from app.agents.generator import run_generator
from app.agents.interpreter import run_interpreter
from app.agents.mapper import run_mapper
from app.agents.tools import clean_sql, validate_sql_syntax
from app.agents.validator import run_validator
from app.db.connection import database_connect


@logfire.instrument("new_pipeline")
async def run_new_pipeline(
    user_message: str,
    server_dsn: str | None = None,
    database: str | None = None,
    execution_id: str | None = None,
    session_id: str | None = None,
) -> PipelineResult:
    """
    Run the new multi-agent text-to-SQL pipeline.

    Args:
        user_message: Natural language question from the user
        server_dsn: Database server DSN (optional, uses config default)
        database: Database name (optional, uses config default)
        execution_id: Optional execution ID for Redis state tracking
        session_id: Optional session ID - if None, generates a new one (for CLI use)

    Returns:
        PipelineResult with all outputs and trace information
    """
    # Generate session_id if not provided (for CLI use)
    if session_id is None:
        from uuid import uuid4

        session_id = str(uuid4())
        logfire.info("Generated new session_id for pipeline", session_id=session_id)

    # Import Redis update functions if execution_id is provided
    update_step = None
    update_status = None
    if execution_id:
        from app.redis_orm import update_execution_status, update_execution_step

        update_step = update_execution_step
        update_status = update_execution_status
    logfire.info(
        "Starting new multi-agent pipeline",
        user_message=user_message,
        server_dsn=server_dsn,
        database=database,
        session_id=session_id,
    )

    # Initialize state with session_id
    state = AgentState(raw_question=user_message, session_id=session_id)

    # Load custom prompts from Redis
    from app.redis_orm import get_prompt_config

    state.custom_prompts = await get_prompt_config()

    # Use provided DSN/DB or fallback to config
    from app.config import db_settings

    final_server_dsn = server_dsn or db_settings.db_url
    final_database = database or db_settings.db_name

    # Initialize trace structure
    pipeline_start_time = time.time()
    state.trace = Trace(
        pipeline="new",
        db_name=final_database,
        start_ts=datetime.now(UTC).isoformat(),
        mapper=mapperTrace(),
    )

    logfire.debug("Database configuration", final_server_dsn=final_server_dsn, final_database=final_database)

    # Establish database connection
    async with database_connect(server_dsn=final_server_dsn, database=final_database) as conn:
        state.database_connection = conn
        logfire.debug("Database connection established")

        # Step 1: Run Agent 1 - Query Interpreter
        logfire.info("Step 1: Running Query Interpreter")
        if update_step and execution_id:
            await update_step(execution_id, "interpreter", "running")
        step_start_time = time.time()
        try:
            interpreter_output = await run_interpreter(state)
            state.interpreter_output = interpreter_output
            state.clarified_question = interpreter_output.clarified_question
            step_timing_ms = int((time.time() - step_start_time) * 1000)
            if update_step and execution_id:
                await update_step(
                    execution_id,
                    "interpreter",
                    "done",
                    output=interpreter_output.model_dump() if interpreter_output else None,
                )

            # Record step in trace
            state.trace.steps.append(
                StepInfo(
                    name="interpreter",
                    timing_ms=step_timing_ms,
                    input_summary={"raw_question": user_message[:200]},
                    output_summary={
                        "clarified_question": interpreter_output.clarified_question[:200],
                        "intent": interpreter_output.explicit_intent[:300]
                        if interpreter_output.explicit_intent
                        else None,
                        "sub_questions_count": len(interpreter_output.sub_questions),
                    },
                )
            )

            logfire.info(
                "Query Interpreter completed",
                clarified_question=interpreter_output.clarified_question,
                sub_question_count=len(interpreter_output.sub_questions),
            )
        except Exception as e:
            step_timing_ms = int((time.time() - step_start_time) * 1000)
            state.trace.steps.append(
                StepInfo(
                    name="interpreter",
                    timing_ms=step_timing_ms,
                    input_summary={"raw_question": user_message[:200]},
                    error=str(e),
                )
            )
            state.trace.end_ts = datetime.now(UTC).isoformat()
            state.trace.latency_ms = int((time.time() - pipeline_start_time) * 1000)
            if update_step and execution_id:
                await update_step(execution_id, "interpreter", "error")
            if update_status and execution_id:
                await update_status(execution_id, "error", error=f"Query Interpreter failed: {str(e)}")
            logfire.error("Query Interpreter failed", error=str(e), error_type=type(e).__name__)
            return PipelineResult(
                status="ERROR",
                error=f"Query Interpreter failed: {str(e)}",
                sql=None,
                trace=state.trace,
            )

        # Step 2: Get all table names upfront and pass to mapper
        logfire.info("Step 2: Getting all table names")
        # Get all tables directly from information_schema
        table_rows = await conn.fetch(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
            ORDER BY table_name
            """
        )
        all_tables = [row["table_name"] for row in table_rows]
        state.scratch["all_tables"] = all_tables
        state.trace.mapper.all_tables_count = len(all_tables)
        logfire.info("Retrieved all tables", table_count=len(all_tables))

        # Step 2.5: Get sample rows for first 20 tables (with text truncation)
        logfire.info("Step 2.5: Fetching sample rows for all tables")
        sample_rows_dict: dict[str, dict[str, Any] | None] = {}

        for table_name in all_tables:
            try:
                # Get column names for this table
                column_rows = await conn.fetch(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = $1
                    ORDER BY ordinal_position
                    """,
                    table_name,
                )
                column_names = [row["column_name"] for row in column_rows]

                if column_names:
                    # Build SELECT query with proper quoting
                    quoted_columns = ", ".join(f'"{col}"' for col in column_names)
                    sample_query = f'SELECT {quoted_columns} FROM "{table_name}" LIMIT 1'
                    sample_rows = await conn.fetch(sample_query)

                    if sample_rows:
                        # Convert row to dict and truncate text columns
                        sample_row = dict(sample_rows[0])
                        truncated_row = {}

                        for key, value in sample_row.items():
                            if value is None:
                                truncated_row[key] = None
                            elif isinstance(value, str):
                                # Truncate text to max 100 characters
                                truncated_row[key] = value[:50] if len(value) > 50 else value
                            elif isinstance(value, (int, float, bool)):
                                truncated_row[key] = value
                            else:
                                # Convert other types to string and truncate
                                str_value = str(value)
                                truncated_row[key] = str_value[:100] if len(str_value) > 100 else str_value

                        sample_rows_dict[table_name] = truncated_row
                    else:
                        sample_rows_dict[table_name] = None
                else:
                    sample_rows_dict[table_name] = None
            except Exception as e:
                logfire.warning("Error fetching sample row", table=table_name, error=str(e))
                sample_rows_dict[table_name] = None

        state.scratch["sample_rows"] = sample_rows_dict
        logfire.info(
            "Sample rows fetched",
            tables_sampled=len(all_tables),
            rows_with_data=sum(1 for v in sample_rows_dict.values() if v is not None),
        )

        # Step 2: Run Agent 2 - Schema mapper
        logfire.info("Step 2: Running Schema mapper")
        if update_step and execution_id:
            await update_step(execution_id, "mapper", "running")
        step_start_time = time.time()
        try:
            mapper_output = await run_mapper(state)
            state.mapper_output = mapper_output
            step_timing_ms = int((time.time() - step_start_time) * 1000)
            if update_step and execution_id:
                await update_step(execution_id, "mapper", "done", output=mapper_output)

            # Record step in trace
            state.trace.steps.append(
                StepInfo(
                    name="mapper",
                    timing_ms=step_timing_ms,
                    input_summary={
                        "clarified_question": state.clarified_question[:200] if state.clarified_question else None,
                        "all_tables_count": len(all_tables),
                    },
                    output_summary={
                        "output_length": len(mapper_output),
                        "output_preview": mapper_output[:300] if len(mapper_output) > 300 else mapper_output,
                    },
                )
            )

            logfire.info(
                "Schema mapper completed",
                output_length=len(mapper_output),
                output_preview=mapper_output[:300] if len(mapper_output) > 300 else mapper_output,
            )
        except Exception as e:
            step_timing_ms = int((time.time() - step_start_time) * 1000)
            state.trace.steps.append(
                StepInfo(
                    name="mapper",
                    timing_ms=step_timing_ms,
                    input_summary={
                        "clarified_question": state.clarified_question[:200] if state.clarified_question else None,
                        "all_tables_count": len(all_tables),
                    },
                    error=str(e),
                )
            )
            state.trace.end_ts = datetime.now(UTC).isoformat()
            state.trace.latency_ms = int((time.time() - pipeline_start_time) * 1000)
            if update_step and execution_id:
                await update_step(execution_id, "mapper", "error")
            if update_status and execution_id:
                await update_status(execution_id, "error", error=f"Schema mapper failed: {str(e)}")
            logfire.error("Schema mapper failed", error=str(e), error_type=type(e).__name__)
            return PipelineResult(
                status="ERROR",
                error=f"Schema mapper failed: {str(e)}",
                sql=None,
                interpreter_output=state.interpreter_output,
                trace=state.trace,
            )

        # Step 3-4: Generator (up to 2 attempts) + Validator (once after first attempt)
        logfire.info("Step 3-4: Starting generator-validator flow")
        best_valid_query: PipelineResult | None = None
        validator_has_run = False

        # Generator can run up to 2 times: first attempt, then if validator rejects, one more attempt
        max_generator_attempts = 2
        for generator_attempt in range(max_generator_attempts):
            state.attempt_count = generator_attempt
            logfire.info("Generator attempt", attempt=generator_attempt + 1, max_attempts=max_generator_attempts)

            # Step 3: Run Agent 3 - SQL Generator
            logfire.info("Step 3: Running SQL Generator", attempt=generator_attempt + 1)
            if update_step and execution_id:
                await update_step(execution_id, "generator", "running")
            step_start_time = time.time()
            try:
                generator_output = await run_generator(state)
                state.generator_output = generator_output
                # Clean SQL before storing
                cleaned_sql = clean_sql(generator_output.sql_query)
                state.current_sql = cleaned_sql
                state.sql_history.append(cleaned_sql)
                step_timing_ms = int((time.time() - step_start_time) * 1000)
                # Update Redis with SQL query as soon as it's generated
                if update_status and execution_id and cleaned_sql:
                    await update_status(execution_id, "running", sql_query=cleaned_sql)
                if update_step and execution_id:
                    await update_step(
                        execution_id,
                        "generator",
                        "done",
                        output=generator_output.model_dump() if generator_output else None,
                    )

                # Record step in trace
                state.trace.steps.append(
                    StepInfo(
                        name="generator",
                        timing_ms=step_timing_ms,
                        input_summary={
                            "clarified_question": state.clarified_question[:200] if state.clarified_question else None,
                            "attempt": generator_attempt + 1,
                        },
                        output_summary={
                            "sql_length": len(generator_output.sql_query),
                            "reasoning_steps_count": len(generator_output.reasoning_steps),
                            "confidence": generator_output.confidence,
                        },
                    )
                )

                # Record generator output in trace
                state.trace.generator = GeneratorTrace(
                    sql_query=cleaned_sql,
                    reasoning_steps=generator_output.reasoning_steps,
                    confidence=generator_output.confidence,
                )

                logfire.info(
                    "SQL Generator completed",
                    attempt=generator_attempt + 1,
                    sql_length=len(generator_output.sql_query),
                    confidence=generator_output.confidence,
                )

                # Pre-validate syntax before calling validator
                if cleaned_sql:
                    syntax_valid, syntax_error = await validate_sql_syntax(conn, cleaned_sql)
                    state.syntax_valid = syntax_valid
                    state.syntax_error = syntax_error
                    logfire.info(
                        "SQL syntax validation",
                        attempt=generator_attempt + 1,
                        syntax_valid=syntax_valid,
                        syntax_error=syntax_error,
                    )

                    # Step 4: Run Agent 4 - Validator (ONLY ONCE, after first generator attempt)
                    if not validator_has_run:
                        logfire.info("Step 4: Running SQL Validator (first and only time)")
                        if update_step and execution_id:
                            await update_step(execution_id, "validator", "running")
                        validator_step_start_time = time.time()
                        try:
                            validator_output = await run_validator(state)
                            state.validator_output = validator_output
                            validator_has_run = True
                            validator_step_timing_ms = int((time.time() - validator_step_start_time) * 1000)
                            # Update Redis with validator output
                            if update_step and execution_id:
                                await update_step(
                                    execution_id,
                                    "validator",
                                    "done",
                                    output=validator_output.model_dump() if validator_output else None,
                                )

                            # Record step in trace
                            state.trace.steps.append(
                                StepInfo(
                                    name="validator",
                                    timing_ms=validator_step_timing_ms,
                                    input_summary={
                                        "sql_length": len(cleaned_sql) if cleaned_sql else 0,
                                        "attempt": generator_attempt + 1,
                                    },
                                    output_summary={
                                        "is_valid": validator_output.is_valid,
                                        "is_optimal": validator_output.is_optimal,
                                        "efficiency_score": validator_output.efficiency_score,
                                        "syntax_error_count": len(validator_output.syntax_errors),
                                        "semantic_issue_count": len(validator_output.semantic_issues),
                                    },
                                )
                            )

                            logfire.info(
                                "SQL Validator completed",
                                is_valid=validator_output.is_valid,
                                is_optimal=validator_output.is_optimal,
                                efficiency_score=validator_output.efficiency_score,
                            )

                            # Track best valid query
                            if validator_output.is_valid:
                                best_valid_query = PipelineResult(
                                    status="GENERATED",
                                    sql=cleaned_sql,
                                    plan=interpreter_output.explicit_intent if interpreter_output else None,
                                    feedback=validator_output.refinement_feedback,
                                    attempts=generator_attempt + 1,
                                    all_queries=state.sql_history.copy(),
                                    interpreter_output=state.interpreter_output,
                                    mapper_output=state.mapper_output,
                                    generator_output=state.generator_output,
                                    validator_output=validator_output,
                                    trace=state.trace,
                                )
                                state.best_sql = cleaned_sql
                                state.best_validator_output = validator_output

                            # If query is valid, return immediately
                            if validator_output.is_valid:
                                state.trace.end_ts = datetime.now(UTC).isoformat()
                                state.trace.latency_ms = int((time.time() - pipeline_start_time) * 1000)
                                logfire.info(
                                    "Query is valid, returning",
                                    is_optimal=validator_output.is_optimal,
                                    efficiency_score=validator_output.efficiency_score,
                                )
                                return PipelineResult(
                                    status="GENERATED",
                                    sql=cleaned_sql,
                                    plan=interpreter_output.explicit_intent if interpreter_output else None,
                                    feedback=validator_output.refinement_feedback,
                                    attempts=generator_attempt + 1,
                                    all_queries=state.sql_history,
                                    interpreter_output=state.interpreter_output,
                                    mapper_output=state.mapper_output,
                                    generator_output=state.generator_output,
                                    validator_output=validator_output,
                                    trace=state.trace,
                                )

                            # If query is invalid and this is the first generator attempt, try generator again
                            if not validator_output.is_valid and generator_attempt == 0:
                                logfire.info(
                                    "Query invalid on first attempt, will retry generator once",
                                    feedback=validator_output.refinement_feedback[:200],
                                )
                                # Continue to next generator attempt (second attempt)
                                continue
                            elif not validator_output.is_valid:
                                # Second generator attempt also invalid, return with error
                                state.trace.end_ts = datetime.now(UTC).isoformat()
                                state.trace.latency_ms = int((time.time() - pipeline_start_time) * 1000)
                                logfire.warning(
                                    "Query invalid after second generator attempt",
                                    last_feedback=validator_output.refinement_feedback[:200],
                                )
                                return PipelineResult(
                                    status="REJECTED",
                                    sql=cleaned_sql,  # Always include SQL even if invalid, so it can be executed to show actual error
                                    error=validator_output.refinement_feedback or "Failed to generate valid query",
                                    attempts=generator_attempt + 1,
                                    all_queries=state.sql_history,
                                    interpreter_output=state.interpreter_output,
                                    mapper_output=state.mapper_output,
                                    generator_output=state.generator_output,
                                    validator_output=validator_output,
                                    trace=state.trace,
                                )

                        except Exception as e:
                            validator_step_timing_ms = int((time.time() - validator_step_start_time) * 1000)
                            validator_has_run = True  # Mark as run even if it failed
                            if update_step and execution_id:
                                await update_step(execution_id, "validator", "error")
                            state.trace.steps.append(
                                StepInfo(
                                    name="validator",
                                    timing_ms=validator_step_timing_ms,
                                    input_summary={
                                        "sql_length": len(cleaned_sql) if cleaned_sql else 0,
                                        "attempt": generator_attempt + 1,
                                    },
                                    error=str(e),
                                )
                            )
                            logfire.error(
                                "SQL Validator failed",
                                error=str(e),
                                error_type=type(e).__name__,
                                attempt=generator_attempt + 1,
                            )
                            # If validator fails, treat as invalid and try generator again if first attempt
                            if generator_attempt == 0:
                                logfire.info("Validator failed on first attempt, will retry generator once")
                                continue
                            else:
                                # Second attempt and validator failed - return with error
                                state.trace.end_ts = datetime.now(UTC).isoformat()
                                state.trace.latency_ms = int((time.time() - pipeline_start_time) * 1000)
                                logfire.warning("Validator failed on second attempt, returning error")
                                return PipelineResult(
                                    status="ERROR",
                                    error=f"SQL Validator failed: {str(e)}",
                                    sql=cleaned_sql if cleaned_sql else None,
                                    attempts=generator_attempt + 1,
                                    all_queries=state.sql_history,
                                    interpreter_output=state.interpreter_output,
                                    mapper_output=state.mapper_output,
                                    generator_output=state.generator_output,
                                    validator_output=None,
                                    trace=state.trace,
                                )
                    else:
                        # Validator has already run (on first attempt), this is second generator attempt
                        # Return the second generator's output without re-validating
                        logfire.info(
                            "Second generator attempt completed, returning without re-validation",
                            sql_length=len(cleaned_sql),
                        )
                        state.trace.end_ts = datetime.now(UTC).isoformat()
                        state.trace.latency_ms = int((time.time() - pipeline_start_time) * 1000)
                        # Use previous validator feedback for context
                        feedback = (
                            state.validator_output.refinement_feedback
                            if state.validator_output
                            else "Query generated on second attempt after first was rejected"
                        )
                        return PipelineResult(
                            status="GENERATED",  # Return as GENERATED even though not re-validated
                            sql=cleaned_sql,
                            plan=interpreter_output.explicit_intent if interpreter_output else None,
                            feedback=feedback,
                            attempts=generator_attempt + 1,
                            all_queries=state.sql_history,
                            interpreter_output=state.interpreter_output,
                            mapper_output=state.mapper_output,
                            generator_output=state.generator_output,
                            validator_output=state.validator_output,  # Include previous validator output
                            trace=state.trace,
                        )
                else:
                    # No SQL generated - try generator again if first attempt
                    state.syntax_valid = False
                    state.syntax_error = "No SQL query generated"
                    logfire.warning("Generator produced empty SQL", attempt=generator_attempt + 1)
                    if generator_attempt == 0:
                        continue  # Try generator again
                    else:
                        # Second attempt also failed to generate SQL
                        state.trace.end_ts = datetime.now(UTC).isoformat()
                        state.trace.latency_ms = int((time.time() - pipeline_start_time) * 1000)
                        if update_step and execution_id:
                            await update_step(execution_id, "generator", "error")
                        if update_status and execution_id:
                            await update_status(execution_id, "error", error="Failed to generate SQL query")
                        return PipelineResult(
                            status="REJECTED",
                            sql=None,
                            error="Failed to generate SQL query",
                            attempts=generator_attempt + 1,
                            all_queries=state.sql_history,
                            interpreter_output=state.interpreter_output,
                            mapper_output=state.mapper_output,
                            generator_output=state.generator_output,
                            validator_output=state.validator_output if validator_has_run else None,
                            trace=state.trace,
                        )
            except Exception as e:
                step_timing_ms = int((time.time() - step_start_time) * 1000)
                state.trace.steps.append(
                    StepInfo(
                        name="generator",
                        timing_ms=step_timing_ms,
                        input_summary={
                            "clarified_question": state.clarified_question[:200] if state.clarified_question else None,
                            "attempt": generator_attempt + 1,
                        },
                        error=str(e),
                    )
                )
                logfire.error(
                    "SQL Generator failed",
                    error=str(e),
                    error_type=type(e).__name__,
                    attempt=generator_attempt + 1,
                )
                if update_step and execution_id:
                    await update_step(execution_id, "generator", "error")
                # Try generator again if first attempt
                if generator_attempt == 0:
                    logfire.info("Generator failed on first attempt, will retry once")
                    continue
                else:
                    # Second attempt also failed - return error
                    state.trace.end_ts = datetime.now(UTC).isoformat()
                    state.trace.latency_ms = int((time.time() - pipeline_start_time) * 1000)
                    if update_status and execution_id:
                        await update_status(execution_id, "error", error=f"SQL Generator failed: {str(e)}")
                    return PipelineResult(
                        status="ERROR",
                        error=f"SQL Generator failed: {str(e)}",
                        sql=state.current_sql,  # Return last generated SQL if available
                        attempts=generator_attempt + 1,
                        all_queries=state.sql_history,
                        interpreter_output=state.interpreter_output,
                        mapper_output=state.mapper_output,
                        generator_output=state.generator_output,
                        trace=state.trace,
                    )

        # Loop finished - return best valid query or error
        if best_valid_query:
            logfire.info(
                "Pipeline completed with best valid query",
                status=best_valid_query.status,
                attempts=best_valid_query.attempts,
                total_queries_generated=len(state.sql_history),
                efficiency_score=best_valid_query.validator_output.efficiency_score
                if best_valid_query.validator_output
                else 0.0,
            )
            best_valid_query.trace.end_ts = datetime.now(UTC).isoformat()
            best_valid_query.trace.latency_ms = int((time.time() - pipeline_start_time) * 1000)
            return best_valid_query

        # No valid query found after both generator attempts
        last_feedback = (
            state.validator_output.refinement_feedback if state.validator_output else "Failed to generate valid query"
        )
        state.trace.end_ts = datetime.now(UTC).isoformat()
        state.trace.latency_ms = int((time.time() - pipeline_start_time) * 1000)
        logfire.warning(
            "Pipeline failed - no valid query found after 2 generator attempts",
            total_queries_generated=len(state.sql_history),
        )
        # Include last generated SQL if available, even if invalid, so it can be executed to show error
        last_sql = state.current_sql if state.current_sql else (state.sql_history[-1] if state.sql_history else None)
        return PipelineResult(
            status="REJECTED",
            error=last_feedback,
            sql=last_sql,
            attempts=2,
            all_queries=state.sql_history,
            interpreter_output=state.interpreter_output,
            mapper_output=state.mapper_output,
            generator_output=state.generator_output,
            validator_output=state.validator_output,
            trace=state.trace,
        )
