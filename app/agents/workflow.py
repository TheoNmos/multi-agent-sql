"""Supervisor-Worker pipeline - Orchestrates the multi-agent text-to-SQL pipeline via supervisor agent."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import logfire

from app.agents.context import AgentState, PipelineResult, Trace, mapperTrace
from app.agents.supervisor import SUPERVISOR_USAGE_LIMITS, supervisor
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
    Run the multi-agent text-to-SQL pipeline via supervisor-worker architecture.

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
        "Starting supervisor-worker pipeline",
        user_message=user_message,
        server_dsn=server_dsn,
        database=database,
        session_id=session_id,
    )

    # Initialize state with session_id
    state = AgentState(raw_question=user_message, session_id=session_id)

    # Store Redis helpers in scratch for supervisor tools
    if execution_id:
        state.scratch["execution_id"] = execution_id
        state.scratch["update_step"] = update_step
        state.scratch["update_status"] = update_status

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

        # Schema prep: get all tables and sample rows (before supervisor)
        logfire.info("Schema prep: Getting all table names")
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

        logfire.info("Schema prep: Fetching sample rows for all tables")
        sample_rows_dict: dict[str, dict[str, Any] | None] = {}
        for table_name in all_tables:
            try:
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
                    quoted_columns = ", ".join(f'"{col}"' for col in column_names)
                    sample_query = f'SELECT {quoted_columns} FROM "{table_name}" LIMIT 1'
                    sample_rows = await conn.fetch(sample_query)
                    if sample_rows:
                        sample_row = dict(sample_rows[0])
                        truncated_row = {}
                        for key, value in sample_row.items():
                            if value is None:
                                truncated_row[key] = None
                            elif isinstance(value, str):
                                truncated_row[key] = value[:50] if len(value) > 50 else value
                            elif isinstance(value, (int, float, bool)):
                                truncated_row[key] = value
                            else:
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

        # Run supervisor agent
        logfire.info("Running supervisor agent")
        try:
            result = await supervisor.run(
                user_message,
                deps=state,
                usage_limits=SUPERVISOR_USAGE_LIMITS,
            )
            supervisor_output = result.output
        except Exception as e:
            logfire.error("Supervisor failed", error=str(e), error_type=type(e).__name__)
            state.trace.end_ts = datetime.now(UTC).isoformat()
            state.trace.latency_ms = int((time.time() - pipeline_start_time) * 1000)
            if update_status and execution_id:
                await update_status(execution_id, "error", error=str(e))
            return PipelineResult(
                status="ERROR",
                error=str(e),
                sql=state.current_sql,
                interpreter_output=state.interpreter_output,
                mapper_output=state.mapper_output,
                generator_output=state.generator_output,
                validator_output=state.validator_output,
                trace=state.trace,
            )

        # Finalize trace
        state.trace.end_ts = datetime.now(UTC).isoformat()
        state.trace.latency_ms = int((time.time() - pipeline_start_time) * 1000)

        # Map SupervisorOutput to PipelineResult
        if supervisor_output.status == "success":
            pipeline_status = "GENERATED"
            error_msg = None
        elif supervisor_output.status == "reject":
            pipeline_status = "REJECTED"
            error_msg = supervisor_output.message
        else:
            pipeline_status = "ERROR"
            error_msg = supervisor_output.message

        return PipelineResult(
            status=pipeline_status,
            sql=supervisor_output.final_sql or state.current_sql,
            error=error_msg,
            plan=state.interpreter_output.explicit_intent if state.interpreter_output else None,
            feedback=state.validator_output.refinement_feedback if state.validator_output else None,
            attempts=len(state.sql_history),
            all_queries=state.sql_history,
            interpreter_output=state.interpreter_output,
            mapper_output=state.mapper_output,
            generator_output=state.generator_output,
            validator_output=state.validator_output,
            trace=state.trace,
        )
