from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import asyncpg
import logfire
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app.config import db_settings, settings
from app.prompts import AGENT_IDS, get_default_prompt
from app.redis_orm import (
    ConnectionString,
    QueryExecution,
    Session,
    get_connection_string,
    get_execution,
    get_prompt_config,
    get_session,
    list_connection_strings,
    list_executions,
    list_executions_by_session,
    list_sessions,
    reset_prompt_config,
    save_connection_string,
    save_execution,
    save_prompt_config,
    save_session,
    update_execution_status,
)


async def _seed_default_connection():
    """Seed a default connection from env vars when running in Docker and no connections exist."""
    try:
        connections = await list_connection_strings()
        if connections:
            return
        # Build full connection string from config (DB_URL + DB_NAME)
        # In Docker, DB_URL is postgresql://user:password@db:5432 (uses service name)
        db_url = db_settings.db_url
        db_name = db_settings.db_name
        if not db_url or not db_name:
            return
        # Build full connection string (e.g. postgresql://user:pass@db:5432/bird_benchmark)
        parsed = urlparse(db_url)
        if parsed.path and parsed.path.strip("/"):
            conn_str = db_url  # Already has database in path
        else:
            conn_str = f"{db_url.rstrip('/')}/{db_name}"
        conn = ConnectionString(connection_string=conn_str, database_name=db_name)
        await save_connection_string(conn)
        logfire.info("Seeded default connection from DB_URL/DB_NAME", database=db_name)
    except Exception as e:
        logfire.warning("Could not seed default connection", error=str(e))


@asynccontextmanager
async def lifespan(app: FastAPI):
    logfire.configure(token=settings.logfire_token)
    logfire.instrument_pydantic_ai()
    logfire.instrument_fastapi(app, excluded_urls=[r"/api/queries.*"])
    await _seed_default_connection()
    yield


app = FastAPI(lifespan=lifespan)
templates = Jinja2Templates(directory="app/templates")


def parse_connection_string(conn_str: str) -> tuple[str, str]:
    """Parse PostgreSQL connection string to extract server DSN and database name."""
    logfire.info(f"Parsing connection string: {conn_str[:50]}...")
    parsed = urlparse(conn_str)
    logfire.info(f"Parsed URL - scheme: {parsed.scheme}, netloc: {parsed.netloc}, path: {parsed.path}")

    # Accept both postgresql and postgres schemes
    if parsed.scheme not in ("postgresql", "postgres"):
        error_msg = f"Connection string must be PostgreSQL (got scheme: {parsed.scheme})"
        logfire.error(error_msg)
        raise ValueError(error_msg)

    # Extract database name from path
    database = parsed.path.lstrip("/") if parsed.path else ""
    logfire.info(f"Extracted database name: {database}")

    # Reconstruct server DSN without database
    server_dsn = f"{parsed.scheme}://{parsed.netloc}"
    logfire.info(f"Server DSN: {server_dsn}")

    return server_dsn, database


# Routes


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Main UI page."""
    logfire.info("Serving index page")
    connections = await list_connection_strings()
    sessions = await list_sessions()
    logfire.info(f"Found {len(connections)} connections and {len(sessions)} sessions")

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "connections": connections,
            "sessions": sessions,
        },
    )


@app.post("/api/connections")
async def create_connection(
    request: Request,
    connection_string: str = Form(),
    database_name: str = Form(),
):
    """Save a new connection string."""
    if not connection_string:
        raise HTTPException(status_code=400, detail="connection_string is required")
    if not database_name:
        raise HTTPException(status_code=400, detail="database_name is required")

    logfire.info(
        f"Received connection creation request - connection_string length: {len(connection_string)}, database_name: {database_name}"
    )

    # Validate connection string format
    try:
        server_dsn, db_name = parse_connection_string(connection_string)
        # Use provided database_name if connection string doesn't have one
        if not db_name:
            db_name = database_name
            logfire.info(f"Using provided database_name: {database_name}")
        else:
            logfire.info(f"Using database from connection string: {db_name}")
    except ValueError as e:
        error_msg = f"Invalid connection string: {str(e)}"
        logfire.error(error_msg)
        raise HTTPException(status_code=400, detail=error_msg)
    except Exception as e:
        error_msg = f"Unexpected error parsing connection string: {str(e)}"
        logfire.error(error_msg, exc_info=True)
        raise HTTPException(status_code=400, detail=error_msg)

    try:
        final_db_name = db_name or database_name
        if not final_db_name:
            error_msg = "Database name is required (either in connection string or as separate field)"
            logfire.error(error_msg)
            raise HTTPException(status_code=400, detail=error_msg)

        logfire.info(f"Creating ConnectionString with database: {final_db_name}")
        connection = ConnectionString(
            connection_string=connection_string,
            database_name=final_db_name,
        )
        conn_id = await save_connection_string(connection)
        logfire.info(f"Successfully saved connection with ID: {conn_id}")

        # Return HTML snippet for HTMX to insert.
        # NOTE: We keep the text translatable client-side via data-i18n attributes.
        from fastapi.responses import HTMLResponse

        return HTMLResponse(
            f"""
            <div class="connection-item" id="conn-{conn_id}">
                <div class="connection-info">
                    <div class="connection-name">{connection.connection_string}</div>
                    <div class="connection-db">
                        <span data-i18n="connection.database_label">Database</span>: {connection.database_name}
                    </div>
                </div>
                <div class="connection-actions">
                    <button class="test-btn" onclick="testConnection('{conn_id}')" id="test-btn-{conn_id}" data-i18n="connection.test_button">Test Connection</button>
                    <span id="test-status-{conn_id}" class="connection-status" style="display: none;"></span>
                </div>
            </div>
        """
        )
    except Exception as e:
        error_msg = f"Error saving connection: {str(e)}"
        logfire.error(error_msg, exc_info=True)
        raise HTTPException(status_code=500, detail=error_msg)


@app.get("/api/connections")
async def list_connections():
    """List all connection strings."""
    logfire.info("Listing all connections")
    connections = await list_connection_strings()
    logfire.info(f"Returning {len(connections)} connections")
    return [
        {
            "id": conn.id,
            "connection_string": conn.connection_string,
            "database_name": conn.database_name,
            "created_at": conn.created_at.isoformat(),
        }
        for conn in connections
    ]


@app.post("/api/connections/{connection_id}/test")
async def test_connection(connection_id: str):
    """Test a database connection."""
    logfire.info(f"Testing connection: {connection_id}")

    # Get connection string
    conn = await get_connection_string(connection_id)
    if conn is None:
        raise HTTPException(status_code=404, detail="Connection not found")

    # Parse connection string
    try:
        server_dsn, db_name = parse_connection_string(conn.connection_string)
        # Fallback to stored database_name, then to config default
        if not db_name:
            db_name = conn.database_name or db_settings.db_name
        if not db_name:
            raise HTTPException(status_code=400, detail="Database name is required")
        logfire.info("Testing connection", db_name=db_name, server_dsn=server_dsn[:50])
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid connection string: {str(e)}")

    # Test connection
    try:
        test_conn = await asyncpg.connect(f"{server_dsn}/{db_name}")
        try:
            # Execute a simple query to verify connection
            result = await test_conn.fetchval("SELECT version()")
            logfire.info(f"Connection test successful for {connection_id}")
            return {
                "success": True,
                "message": "Connection successful",
                "database_version": result,
            }
        finally:
            await test_conn.close()
    except Exception as e:
        error_msg = str(e)
        logfire.error(f"Connection test failed for {connection_id}: {error_msg}")
        return {
            "success": False,
            "message": f"Connection failed: {error_msg}",
        }


@app.post("/api/sessions")
async def create_session_endpoint(request: Request, name: str | None = Form(None)):
    """Create a new session."""
    session = Session(name=name)
    session_id = await save_session(session)
    logfire.info(f"Created new session: {session_id}")
    return JSONResponse(content={"session_id": session_id, "name": session.name})


@app.get("/api/sessions")
async def get_sessions_endpoint():
    """Get all sessions."""
    sessions = await list_sessions()
    return [
        {
            "id": s.id,
            "name": s.name,
            "execution_count": len(s.execution_ids),
            "created_at": s.created_at.isoformat(),
            "updated_at": s.updated_at.isoformat(),
        }
        for s in sessions
    ]


@app.get("/api/sessions/{session_id}")
async def get_session_details_endpoint(session_id: str):
    """Get session details with all executions."""
    session = await get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    executions = await list_executions_by_session(session_id)
    return {
        "id": session.id,
        "name": session.name,
        "created_at": session.created_at.isoformat(),
        "updated_at": session.updated_at.isoformat(),
        "executions": [
            {
                "id": e.id,
                "user_query": e.user_query,
                "status": e.status,
                "current_step": e.current_step,
                "step_status": e.step_status,
                "sql_query": e.sql_query,
                "error": e.error,
                "created_at": e.created_at.isoformat(),
                "updated_at": e.updated_at.isoformat(),
            }
            for e in executions
        ],
    }


@app.get("/api/sessions/{session_id}/executions")
async def get_session_executions_endpoint(session_id: str):
    """Get all executions for a session."""
    executions = await list_executions_by_session(session_id)
    return [
        {
            "id": e.id,
            "user_query": e.user_query,
            "status": e.status,
            "current_step": e.current_step,
            "step_status": e.step_status,
            "sql_query": e.sql_query,
            "query_result": e.query_result,
            "error": e.error,
            "interpreter_output": e.interpreter_output,
            "mapper_output": e.mapper_output,
            "generator_output": e.generator_output,
            "validator_output": e.validator_output,
            "created_at": e.created_at.isoformat(),
            "updated_at": e.updated_at.isoformat(),
        }
        for e in executions
    ]


# Prompt Config API


@app.get("/api/prompts")
async def get_prompts_endpoint():
    """Get all prompts (defaults merged with custom; indicates which are customized)."""
    custom = await get_prompt_config()
    prompts = []
    for agent_id in AGENT_IDS:
        prompt = custom.get(agent_id) or get_default_prompt(agent_id)
        prompts.append(
            {
                "agent_id": agent_id,
                "prompt": prompt,
                "is_customized": agent_id in custom,
            }
        )
    return {"prompts": prompts}


@app.get("/api/prompts/default")
async def get_default_prompt_endpoint(agent_id: str):
    """Get the default prompt for an agent (for Reset preview)."""
    if agent_id not in AGENT_IDS:
        raise HTTPException(status_code=400, detail=f"Invalid agent_id. Valid: {AGENT_IDS}")
    return {"prompt": get_default_prompt(agent_id)}


@app.put("/api/prompts")
async def save_prompt_endpoint(request: Request):
    """Save a custom prompt for an agent."""
    body = await request.json()
    agent_id = body.get("agent_id")
    prompt = body.get("prompt")
    if not agent_id:
        raise HTTPException(status_code=400, detail="agent_id is required")
    if prompt is None:
        raise HTTPException(status_code=400, detail="prompt is required")
    if agent_id not in AGENT_IDS:
        raise HTTPException(status_code=400, detail=f"Invalid agent_id. Valid: {AGENT_IDS}")
    try:
        await save_prompt_config(agent_id, prompt)
        return {"status": "saved", "agent_id": agent_id}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/prompts/{agent_id}")
async def reset_prompt_endpoint(agent_id: str):
    """Reset an agent's prompt to default."""
    if agent_id not in AGENT_IDS:
        raise HTTPException(status_code=400, detail=f"Invalid agent_id. Valid: {AGENT_IDS}")
    try:
        await reset_prompt_config(agent_id)
        return {"status": "reset", "agent_id": agent_id}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/queries")
async def create_query(
    request: Request,
    connection_id: str = Form(),
    user_query: str = Form(),
    session_id: str | None = Form(None),
):
    """Create a new query execution in a session."""
    if not connection_id:
        raise HTTPException(status_code=400, detail="connection_id is required")
    if not user_query:
        raise HTTPException(status_code=400, detail="user_query is required")

    logfire.info(f"Received query request - connection_id: {connection_id}, query length: {len(user_query)}")

    # Create or get session
    if session_id:
        session = await get_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        logfire.info(f"Using existing session: {session_id}")
    else:
        # Create new session automatically if none provided
        session = Session(name=f"Query: {user_query[:50]}")
        session_id = await save_session(session)
        logfire.info(f"Auto-created session: {session_id}")

    # Get connection string
    conn = await get_connection_string(connection_id)
    if conn is None:
        logfire.error(f"Connection not found: {connection_id}")
        raise HTTPException(status_code=404, detail="Connection not found")

    logfire.info(f"Found connection: {conn.connection_string}")

    # Parse connection string
    try:
        server_dsn, db_name = parse_connection_string(conn.connection_string)
        # Fallback to stored database_name, then to config default
        if not db_name:
            db_name = conn.database_name or db_settings.db_name
        # Ensure we have a valid database name
        if not db_name:
            raise HTTPException(status_code=400, detail="Database name is required")
        logfire.info("Using database", db_name=db_name, server_dsn=server_dsn[:50])
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid connection string: {str(e)}")

    # Create execution record with session_id
    execution = QueryExecution(
        session_id=session_id,
        connection_id=connection_id,
        user_query=user_query,
        status="pending",
    )
    exec_id = await save_execution(execution)

    # Start pipeline execution in background
    asyncio.create_task(
        run_pipeline_with_updates(
            exec_id=exec_id,
            user_message=user_query,
            server_dsn=server_dsn,
            database=db_name,
            session_id=session_id,
        )
    )

    return JSONResponse(content={"execution_id": exec_id, "session_id": session_id, "status": "pending"})


@app.get("/api/queries/{execution_id}/status")
async def get_query_status(execution_id: str):
    """Get query execution status (for polling)."""
    execution = await get_execution(execution_id)
    if execution is None:
        raise HTTPException(status_code=404, detail="Execution not found")

    return {
        "id": execution.id,
        "status": execution.status,
        "current_step": execution.current_step,
        "step_status": execution.step_status,
        "sql_query": execution.sql_query,
        "error": execution.error,
        "interpreter_output": execution.interpreter_output,
        "mapper_output": execution.mapper_output,
        "generator_output": execution.generator_output,
        "validator_output": execution.validator_output,
        "updated_at": execution.updated_at.isoformat(),
    }


@app.get("/api/queries/{execution_id}")
async def get_query(execution_id: str):
    """Get full query execution details."""
    execution = await get_execution(execution_id)
    if execution is None:
        raise HTTPException(status_code=404, detail="Execution not found")

    return {
        "id": execution.id,
        "session_id": execution.session_id,
        "connection_id": execution.connection_id,
        "user_query": execution.user_query,
        "status": execution.status,
        "current_step": execution.current_step,
        "step_status": execution.step_status,
        "sql_query": execution.sql_query,
        "query_result": execution.query_result,
        "error": execution.error,
        "interpreter_output": execution.interpreter_output,
        "mapper_output": execution.mapper_output,
        "generator_output": execution.generator_output,
        "validator_output": execution.validator_output,
        "created_at": execution.created_at.isoformat(),
        "updated_at": execution.updated_at.isoformat(),
    }


@app.get("/api/queries/latest")
async def get_latest_execution():
    """Get the latest execution (for restoring state on page load)."""
    executions = await list_executions(limit=1)
    if not executions:
        return {"execution": None}

    exec = executions[0]
    return {
        "execution": {
            "id": exec.id,
            "connection_id": exec.connection_id,
            "user_query": exec.user_query,
            "status": exec.status,
            "current_step": exec.current_step,
            "step_status": exec.step_status,
            "sql_query": exec.sql_query,
            "query_result": exec.query_result,
            "error": exec.error,
            "created_at": exec.created_at.isoformat(),
            "updated_at": exec.updated_at.isoformat(),
        }
    }


# Pipeline Integration


async def run_pipeline_with_updates(exec_id: str, user_message: str, server_dsn: str, database: str, session_id: str):
    """Run pipeline with Redis state updates at each step."""
    from app.agents.workflow import run_new_pipeline

    try:
        # Update status to running
        await update_execution_status(exec_id, "running")

        # Run the compositor pipeline - it will update Redis at each step
        result = await run_new_pipeline(
            user_message=user_message,
            server_dsn=server_dsn,
            database=database,
            execution_id=exec_id,
            session_id=session_id,
        )

        # Update final status based on result
        sql_query = result.sql or ""

        # Always try to execute the query if SQL is available, even if validator marked it as invalid
        # This allows users to see the actual SQL execution errors
        print(f"SQL query: {sql_query}")
        if sql_query:
            print(f"Executing query: {sql_query}")
            query_result = await execute_query(server_dsn, database, sql_query)
            print(f"Query result: {query_result}")
            if result.status == "GENERATED":
                await update_execution_status(
                    exec_id,
                    "completed",
                    sql_query=sql_query,
                    query_result=query_result,
                )
            else:
                # Query was rejected/invalid, but we executed it anyway to show the error
                # Check if execution failed (error in result)
                execution_error = None
                if query_result and len(query_result) == 1 and "error" in query_result[0]:
                    execution_error = query_result[0]["error"]

                # Combine validator feedback with execution error if available
                error_message = result.error or "Query was rejected by validator"
                if execution_error:
                    error_message = f"{error_message}\n\nSQL Execution Error: {execution_error}"

                await update_execution_status(
                    exec_id,
                    "error",
                    sql_query=sql_query,
                    query_result=query_result,
                    error=error_message,
                )
        elif result.status == "ERROR":
            await update_execution_status(exec_id, "error", error=result.error or "Unknown error")
        else:
            await update_execution_status(exec_id, "error", error=result.error or "Pipeline failed")

    except Exception as e:
        logfire.error("Pipeline execution failed", error=str(e), exc_info=True)
        await update_execution_status(exec_id, "error", error=str(e))


async def execute_query(server_dsn: str, database: str, sql_query: str) -> list[dict[str, Any]]:
    """Execute SQL query and return results as list of dicts.

    Uses the same connection method as the agents to ensure consistency.
    """
    from app.agents.tools import clean_sql
    from app.db.connection import database_connect

    try:
        # Clean the SQL query (remove escaped newlines, trailing semicolons, etc.)
        sql_clean = clean_sql(sql_query)

        # Ensure database name is not empty
        if not database:
            logfire.error("Database name is empty", server_dsn=server_dsn)
            return [{"error": "Database name is required"}]

        logfire.info(
            "Executing query", server_dsn=server_dsn[:50] + "...", database=database, sql_preview=sql_clean[:100]
        )

        # Use the same connection method as agents (database_connect)
        async with database_connect(server_dsn=server_dsn, database=database) as conn:
            rows = await conn.fetch(sql_clean)
            # Convert rows to list of dicts
            result = []
            for row in rows:
                result.append(dict(row))
            logfire.info("Query executed successfully", row_count=len(result))
            return result
    except asyncpg.exceptions.PostgresSyntaxError as e:
        error_msg = f"SQL syntax error: {str(e)}"
        print(error_msg)
        logfire.error("SQL syntax error", error=error_msg, sql_preview=sql_query[:200])
        return [{"error": error_msg}]
    except asyncpg.exceptions.PostgresError as e:
        print(e)
        error_msg = f"Database error: {str(e)}"
        print(error_msg)
        logfire.error("Database error", error=error_msg, sql_preview=sql_query[:200])
        return [{"error": error_msg}]
    except Exception as e:
        error_msg = f"Unexpected error: {str(e)}"
        logfire.error("Error executing query", error=error_msg, exc_info=True, sql_preview=sql_query[:200])
        return [{"error": error_msg}]


# Benchmark Endpoints


@app.get("/api/benchmarks/datasets")
async def list_datasets():
    """List available benchmark datasets."""
    return [
        {"id": "bird", "name": "BIRD", "description": "Big Integer Real-world Database"},
        {"id": "livraria", "name": "Livraria", "description": "Synthetic library database benchmark"},
    ]


@app.get("/api/benchmarks/{dataset}/questions")
async def get_benchmark_questions(dataset: str):
    """Load and return questions from benchmark.json for a dataset."""
    from app.benchmarks.datasets import load_bird, load_livraria

    try:
        if dataset == "bird":
            items = load_bird()
            # Transform to consistent format
            questions = []
            for idx, item in enumerate(items, start=1):
                questions.append(
                    {
                        "index": idx,
                        "question_id": item.get("question_id"),
                        "db_id": item.get("db_id"),
                        "question": item.get("question", ""),
                        "difficulty": item.get("difficulty"),
                        "evidence": item.get("evidence"),
                    }
                )
        elif dataset == "livraria":
            items = load_livraria()
            questions = []
            for idx, item in enumerate(items, start=1):
                questions.append(
                    {
                        "index": idx,
                        "question_id": item.get("question_id"),
                        "db_id": None,
                        "question": item.get("pergunta", ""),
                        "difficulty": None,
                        "evidence": None,
                    }
                )
        else:
            raise HTTPException(status_code=400, detail=f"Unknown dataset: {dataset}")

        return {
            "dataset": dataset,
            "total": len(questions),
            "questions": questions,
        }
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=f"Dataset file not found: {str(e)}")
    except Exception as e:
        logfire.error(f"Error loading dataset {dataset}", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error loading dataset: {str(e)}")


@app.post("/api/benchmarks/run")
async def run_benchmark_endpoint(
    request: Request,
    dataset: str = Form(),
    indices: str = Form(),  # Comma-separated or range string like "1,2,3" or "1-5"
    connection_id: str = Form(),
    metrics: str = Form(default="em,exa"),  # Comma-separated: "em", "exa", or "em,exa"
    timeout_s: int = Form(default=30),
    max_concurrent: int = Form(default=5),
):
    """Run benchmark on selected questions."""
    from app.benchmarks.datasets import load_bird, load_livraria, parse_indices
    from app.benchmarks.runner import run_benchmark

    # Validate dataset
    if dataset not in ("bird", "livraria"):
        raise HTTPException(status_code=400, detail=f"Invalid dataset: {dataset}")

    # Get connection
    conn = await get_connection_string(connection_id)
    if conn is None:
        raise HTTPException(status_code=404, detail="Connection not found")

    # Parse connection string to get server_dsn
    try:
        server_dsn, db_name = parse_connection_string(conn.connection_string)
        # Fallback to stored database_name, then to config default
        if not db_name:
            db_name = conn.database_name or db_settings.db_name
        if not db_name:
            raise HTTPException(status_code=400, detail="Database name is required")
        logfire.info("Using database for benchmark", db_name=db_name, server_dsn=server_dsn[:50])
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid connection string: {str(e)}")

    # Load dataset to get max index
    if dataset == "bird":
        all_items = load_bird()
    else:
        all_items = load_livraria()

    max_index = len(all_items)

    # Parse indices
    try:
        index_list = parse_indices(indices, max_index)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid indices: {str(e)}")

    # Parse metrics
    metrics_list = [m.strip().lower() for m in metrics.split(",")]
    valid_metrics = {"em", "exa"}
    invalid_metrics = set(metrics_list) - valid_metrics
    if invalid_metrics:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid metrics: {invalid_metrics}. Valid options: {valid_metrics}",
        )

    # Determine output path
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_dir = Path("benchmark_results")
    output_dir.mkdir(exist_ok=True)
    output_path = str(output_dir / f"{dataset}-{timestamp}.json")

    # Run benchmark in background
    async def run_benchmark_task():
        try:
            results = await run_benchmark(
                dataset=dataset,
                indices=index_list,
                metrics=metrics_list,
                server_dsn=server_dsn,
                db_name=db_name,
                timeout_s=timeout_s,
                use_gold_as_pred=False,
                output_path=output_path,
                max_concurrent=max_concurrent,
            )
            logfire.info(f"Benchmark completed: {len(results)} results written to {output_path}")
        except Exception as e:
            logfire.error(f"Benchmark execution failed", error=str(e), exc_info=True)

    asyncio.create_task(run_benchmark_task())

    return JSONResponse(
        content={
            "status": "started",
            "dataset": dataset,
            "indices": index_list,
            "total_questions": len(index_list),
            "output_path": output_path,
            "metrics": metrics_list,
        }
    )


@app.get("/api/benchmarks/results")
async def list_benchmark_results():
    """List available benchmark result files."""
    results_dir = Path("benchmark_results")
    if not results_dir.exists():
        return []

    result_files = []
    for file_path in sorted(results_dir.glob("*.json"), reverse=True):
        try:
            # Try to read metadata from file
            with open(file_path, encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list) and len(data) > 0:
                    first_result = data[0]
                    result_files.append(
                        {
                            "filename": file_path.name,
                            "dataset": first_result.get("dataset", "unknown"),
                            "total_questions": len(data),
                            "created_at": datetime.fromtimestamp(file_path.stat().st_mtime).isoformat(),
                            "size_bytes": file_path.stat().st_size,
                        }
                    )
        except Exception as e:
            logfire.warning(f"Error reading result file {file_path}: {e}")
            # Still include it but with minimal info
            result_files.append(
                {
                    "filename": file_path.name,
                    "dataset": "unknown",
                    "total_questions": 0,
                    "created_at": datetime.fromtimestamp(file_path.stat().st_mtime).isoformat(),
                    "size_bytes": file_path.stat().st_size,
                }
            )

    return result_files


@app.get("/api/benchmarks/results/{filename}")
async def get_benchmark_result(filename: str):
    """Load a specific benchmark result file."""
    results_dir = Path("benchmark_results")
    file_path = results_dir / filename

    # Security: prevent directory traversal
    if not file_path.resolve().is_relative_to(results_dir.resolve()):
        raise HTTPException(status_code=400, detail="Invalid filename")

    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Result file not found")

    try:
        with open(file_path, encoding="utf-8") as f:
            results = json.load(f)

        # Calculate summary statistics
        total = len(results)
        em_correct = sum(1 for r in results if r.get("exact_match", False))
        em_rate = (em_correct / total * 100) if total > 0 else 0.0

        exa_results = [r.get("execution_match") for r in results if r.get("execution_match") is not None]
        exa_correct = sum(1 for r in exa_results if r is True)
        exa_total = len(exa_results)
        exa_rate = (exa_correct / exa_total * 100) if exa_total > 0 else 0.0

        # Calculate analyzer_match from analyzer_match field or analyzer_output.analyzer_match
        am_results = []
        for r in results:
            am_value = r.get("analyzer_match")
            # If analyzer_match is not directly available, try to get it from analyzer_output
            if am_value is None and r.get("analyzer_output"):
                analyzer_output = r.get("analyzer_output")
                if isinstance(analyzer_output, dict):
                    am_value = analyzer_output.get("analyzer_match")
            if am_value is not None:
                am_results.append(am_value)

        am_correct = sum(1 for r in am_results if r is True)
        am_total = len(am_results)
        am_rate = (am_correct / am_total * 100) if am_total > 0 else 0.0

        errors = sum(1 for r in results if r.get("error") is not None)

        return {
            "filename": filename,
            "results": results,
            "summary": {
                "total": total,
                "exact_match": {
                    "correct": em_correct,
                    "total": total,
                    "rate": em_rate,
                },
                "execution_match": {
                    "correct": exa_correct,
                    "total": exa_total,
                    "rate": exa_rate,
                },
                "analyzer_match": {
                    "correct": am_correct,
                    "total": am_total,
                    "rate": am_rate,
                }
                if am_total > 0
                else None,
                "errors": errors,
            },
        }
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON file: {str(e)}")
    except Exception as e:
        logfire.error(f"Error reading result file {filename}", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error reading result file: {str(e)}")
