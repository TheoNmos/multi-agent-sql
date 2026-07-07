from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlparse

import logfire
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.analytics import (
    close_analytics,
    extract_feedback_snapshot,
    get_feedback,
    get_versus_feedback,
    init_analytics,
    is_analytics_ready,
    save_feedback,
)
from app.auth.dependencies import CurrentUserDep, require_user_or_redirect
from app.auth.routes import router as auth_router
from app.config import auth_settings, db_settings, feature_settings, settings
from app.connections.store import delete_connection, get_connection, list_connections, save_connection
from app.db.dialects import detect_dialect, split_url_and_database
from app.prompts import AGENT_IDS, get_default_prompt
from app.redis_orm import (
    QueryExecution,
    Session,
    delete_execution,
    delete_session,
    get_execution,
    get_prompt_config,
    get_session,
    list_executions,
    list_executions_by_session,
    list_sessions,
    reset_prompt_config,
    save_execution,
    save_prompt_config,
    save_session,
    update_execution_metrics,
    update_execution_status,
)


async def _seed_default_connection():
    """Seed a default connection for dev mode when AUTH_DISABLED is enabled."""
    try:
        if not auth_settings.auth_disabled:
            return
        if not getattr(db_settings, "seed_default_connection", False):
            return
        from app.auth import db as auth_db

        if not auth_db.is_app_db_ready():
            return
        dev_user = await auth_db.ensure_dev_user()
        connections = await list_connections(dev_user.id)
        if connections:
            return
        db_url = db_settings.db_url
        db_name = db_settings.db_name
        if not db_url or not db_name:
            return
        parsed = urlparse(db_url)
        if parsed.path and parsed.path.strip("/"):
            conn_str = db_url
        else:
            conn_str = f"{db_url.rstrip('/')}/{db_name}"
        await save_connection(dev_user.id, conn_str, db_name, label="Default")
        logfire.info("Seeded default connection for dev user", database=db_name)
    except Exception as e:
        logfire.warning("Could not seed default connection", error=str(e))


@asynccontextmanager
async def lifespan(app: FastAPI):
    logfire.configure(token=settings.logfire_token)
    logfire.instrument_pydantic_ai()
    logfire.instrument_fastapi(app, excluded_urls=[r"/api/queries.*"])
    await init_analytics()
    await _seed_default_connection()
    try:
        yield
    finally:
        await close_analytics()


app = FastAPI(lifespan=lifespan)
app.include_router(auth_router)
templates = Jinja2Templates(directory="app/templates")


class FeedbackRequest(BaseModel):
    value: Literal["positive", "negative", "pipeline", "single"]


def _normalize_pipeline_mode(mode: str | None) -> str:
    """Normalize old persisted mode names to the current UI/API names."""
    return "pipeline" if mode in (None, "", "supervisor") else mode


def parse_connection_string(conn_str: str) -> tuple[str, str]:
    """Parse a database connection string into a server DSN and database name.

    Accepts PostgreSQL (``postgres``/``postgresql``) and MySQL
    (``mysql``/``mysql+asyncmy``/``mysql+pymysql``/``mysql+aiomysql``) URLs.
    """
    logfire.info(f"Parsing connection string: {conn_str[:50]}...")
    try:
        dialect = detect_dialect(conn_str)
    except ValueError as e:
        logfire.error(str(e))
        raise

    server_dsn, database = split_url_and_database(conn_str)
    logfire.info(
        "Parsed connection string",
        dialect=dialect,
        database=database,
        server_dsn=server_dsn[:60] if server_dsn else "",
    )
    return server_dsn, database


def _feedback_enabled() -> bool:
    return feature_settings.enable_feedback and is_analytics_ready()


def _serialize_execution(execution: QueryExecution) -> dict[str, Any]:
    result = {
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
        "analyzer_output": execution.analyzer_output,
        "single_agent_tool_calls": execution.single_agent_tool_calls,
        "pipeline_tool_calls": execution.pipeline_tool_calls,
        "pipeline_mode": _normalize_pipeline_mode(execution.pipeline_mode),
        "usage": execution.usage,
        "model_name": execution.model_name,
        "current_activity": execution.current_activity,
        "latency_ms": execution.latency_ms,
        "parent_execution_id": execution.parent_execution_id,
        "comparison_execution_ids": execution.comparison_execution_ids,
        "created_at": execution.created_at.isoformat(),
        "updated_at": execution.updated_at.isoformat(),
    }
    return result


async def _serialize_execution_with_feedback(execution: QueryExecution) -> dict[str, Any]:
    result = _serialize_execution(execution)
    feedback_enabled = _feedback_enabled()
    result["feedback_enabled"] = feedback_enabled
    result["user_feedback"] = (
        await get_feedback(execution.id) if feedback_enabled and result["pipeline_mode"] != "versus" else None
    )
    return result


def _derive_versus_status(executions: list[QueryExecution]) -> str:
    statuses = {execution.status for execution in executions}
    if not statuses:
        return "pending"
    if "running" in statuses:
        return "running"
    if statuses == {"pending"}:
        return "pending"
    if "completed" in statuses:
        return "completed"
    if statuses == {"error"}:
        return "error"
    return "error"


async def _serialize_execution_with_comparison(execution: QueryExecution) -> dict[str, Any]:
    result = await _serialize_execution_with_feedback(execution)
    if _normalize_pipeline_mode(execution.pipeline_mode) != "versus" or not execution.comparison_execution_ids:
        return result

    comparison_ids = execution.comparison_execution_ids
    pipeline_execution = await get_execution(comparison_ids.get("pipeline", ""))
    single_execution = await get_execution(comparison_ids.get("single", ""))
    children = [child for child in (pipeline_execution, single_execution) if child is not None]

    result["status"] = _derive_versus_status(children)
    result["versus_state"] = {
        "pipeline": await _serialize_execution_with_feedback(pipeline_execution) if pipeline_execution else None,
        "single": await _serialize_execution_with_feedback(single_execution) if single_execution else None,
    }
    result["versus_feedback"] = (
        await get_versus_feedback(comparison_ids.get("pipeline", ""), comparison_ids.get("single", ""))
        if _feedback_enabled()
        else None
    )
    result["user_feedback"] = result["versus_feedback"]
    result["current_activity"] = (
        "Running versus comparison" if result["status"] == "running" else execution.current_activity
    )
    return result


# Routes


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Main UI page."""
    user_or_redirect = await require_user_or_redirect(request)
    if isinstance(user_or_redirect, RedirectResponse):
        return user_or_redirect
    user = user_or_redirect
    logfire.info("Serving index page", username=user.username)
    connections = await list_connections(user.id)
    sessions = await list_sessions(user.id)
    logfire.info(f"Found {len(connections)} connections and {len(sessions)} sessions")

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "request": request,
            "connections": connections,
            "sessions": sessions,
            "current_user": user,
            "benchmarks_enabled": feature_settings.enable_benchmarks,
            "feedback_enabled": _feedback_enabled(),
        },
    )


@app.post("/api/connections")
async def create_connection(
    request: Request,
    user: CurrentUserDep,
    connection_string: Annotated[str, Form()],
    database_name: Annotated[str, Form()],
):
    """Save a new connection string."""
    if not connection_string:
        raise HTTPException(status_code=400, detail="connection_string is required")
    if not database_name:
        raise HTTPException(status_code=400, detail="database_name is required")

    logfire.info(
        "Received connection creation request",
        extra={"database_name": database_name, "user_id": user.id},
    )

    # Validate connection string format
    try:
        _server_dsn, db_name = parse_connection_string(connection_string)
        if not db_name:
            db_name = database_name
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
            raise HTTPException(status_code=400, detail="Database name is required")

        connection = await save_connection(
            user.id,
            connection_string,
            final_db_name,
        )
        conn_id = connection.id
        logfire.info("Successfully saved connection", extra={"conn_id": conn_id, "user_id": user.id})

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
                    <button type="button" class="test-btn" onclick="testConnection('{conn_id}')" id="test-btn-{conn_id}" data-i18n="connection.test_button">Test Connection</button>
                    <button type="button" class="delete-btn" onclick="deleteConnection('{conn_id}')" data-i18n="common.delete">Delete</button>
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
async def list_connections_endpoint(user: CurrentUserDep):
    """List all connection strings for the current user."""
    connections = await list_connections(user.id)
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
async def test_connection(connection_id: str, user: CurrentUserDep):
    """Test a database connection."""
    logfire.info("Testing connection", connection_id=connection_id, user_id=user.id)

    conn = await get_connection(user.id, connection_id)
    if conn is None:
        raise HTTPException(status_code=404, detail="Connection not found")

    try:
        server_dsn, db_name = parse_connection_string(conn.connection_string)
        if not db_name:
            db_name = conn.database_name or db_settings.db_name
        if not db_name:
            raise HTTPException(status_code=400, detail="Database name is required")
        logfire.info("Testing connection", db_name=db_name, user_id=user.id)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid connection string: {str(e)}")

    from app.db.connection import database_connect

    try:
        async with database_connect(server_dsn=server_dsn, database=db_name) as adapter:
            version = await adapter.server_version()
            logfire.info(
                "Connection test successful",
                connection_id=connection_id,
                dialect=adapter.dialect,
            )
            return {
                "success": True,
                "message": "Connection successful",
                "database_version": version,
                "dialect": adapter.dialect,
            }
    except Exception as e:
        error_msg = str(e)
        logfire.error("Connection test failed", connection_id=connection_id, error=error_msg)
        return {
            "success": False,
            "message": f"Connection failed: {error_msg}",
        }


@app.delete("/api/connections/{connection_id}")
async def delete_connection_endpoint(connection_id: str, user: CurrentUserDep):
    """Delete a saved database connection."""
    conn = await get_connection(user.id, connection_id)
    if conn is None:
        raise HTTPException(status_code=404, detail="Connection not found")
    deleted = await delete_connection(user.id, connection_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Connection not found")
    logfire.info("Deleted connection", connection_id=connection_id, user_id=user.id)
    return {"success": True}


@app.post("/api/sessions")
async def create_session_endpoint(
    request: Request,
    user: CurrentUserDep,
    name: Annotated[str | None, Form()] = None,
):
    """Create a new session."""
    session = Session(user_id=user.id, name=name)
    session_id = await save_session(session)
    logfire.info("Created new session", session_id=session_id, user_id=user.id)
    return JSONResponse(content={"session_id": session_id, "name": session.name})


@app.get("/api/sessions")
async def get_sessions_endpoint(user: CurrentUserDep):
    """Get all sessions for the current user."""
    sessions = await list_sessions(user.id)
    result = []
    for session in sessions:
        execution_count = 0
        for exec_id in session.execution_ids:
            execution = await get_execution(exec_id, user_id=user.id)
            if execution and execution.parent_execution_id is None:
                execution_count += 1
        result.append(
            {
                "id": session.id,
                "name": session.name,
                "execution_count": execution_count,
                "created_at": session.created_at.isoformat(),
                "updated_at": session.updated_at.isoformat(),
            }
        )
    return result


@app.get("/api/sessions/{session_id}")
async def get_session_details_endpoint(session_id: str, user: CurrentUserDep):
    """Get session details with all executions."""
    session = await get_session(session_id, user_id=user.id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    executions = await list_executions_by_session(user.id, session_id)
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


@app.delete("/api/sessions/{session_id}")
async def delete_session_endpoint(session_id: str, user: CurrentUserDep):
    """Delete a session and all of its query executions."""
    session = await get_session(session_id, user_id=user.id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    deleted = await delete_session(user.id, session_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Session not found")
    logfire.info("Deleted session", session_id=session_id, user_id=user.id)
    return {"success": True}


@app.get("/api/sessions/{session_id}/executions")
async def get_session_executions_endpoint(session_id: str, user: CurrentUserDep):
    """Get all executions for a session."""
    session = await get_session(session_id, user_id=user.id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    executions = await list_executions_by_session(user.id, session_id)
    result = []
    for execution in executions:
        result.append(await _serialize_execution_with_comparison(execution))
    return result


# Prompt Config API


@app.get("/api/prompts")
async def get_prompts_endpoint(user: CurrentUserDep):
    """Get all prompts (defaults merged with custom; indicates which are customized)."""
    custom = await get_prompt_config(user.id)
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
async def get_default_prompt_endpoint(agent_id: str, user: CurrentUserDep):
    """Get the default prompt for an agent (for Reset preview)."""
    if agent_id not in AGENT_IDS:
        raise HTTPException(status_code=400, detail=f"Invalid agent_id. Valid: {AGENT_IDS}")
    return {"prompt": get_default_prompt(agent_id)}


@app.put("/api/prompts")
async def save_prompt_endpoint(request: Request, user: CurrentUserDep):
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
        await save_prompt_config(user.id, agent_id, prompt)
        return {"status": "saved", "agent_id": agent_id}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/prompts/{agent_id}")
async def reset_prompt_endpoint(agent_id: str, user: CurrentUserDep):
    """Reset an agent's prompt to default."""
    if agent_id not in AGENT_IDS:
        raise HTTPException(status_code=400, detail=f"Invalid agent_id. Valid: {AGENT_IDS}")
    try:
        await reset_prompt_config(user.id, agent_id)
        return {"status": "reset", "agent_id": agent_id}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/queries")
async def create_query(
    request: Request,
    user: CurrentUserDep,
    connection_id: Annotated[str, Form()],
    user_query: Annotated[str, Form()],
    session_id: Annotated[str | None, Form()] = None,
    pipeline_mode: Annotated[str, Form()] = "pipeline",  # "pipeline" | "single" | "versus"
):
    """Create a new query execution in a session."""
    if not connection_id:
        raise HTTPException(status_code=400, detail="connection_id is required")
    if not user_query:
        raise HTTPException(status_code=400, detail="user_query is required")

    logfire.info(
        "Received query request",
        extra={"connection_id": connection_id, "query_length": len(user_query), "user_id": user.id},
    )

    if session_id:
        session = await get_session(session_id, user_id=user.id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        logfire.info("Using existing session", session_id=session_id, user_id=user.id)
    else:
        session = Session(user_id=user.id, name=f"Query: {user_query[:50]}")
        session_id = await save_session(session)
        logfire.info("Auto-created session", session_id=session_id, user_id=user.id)

    conn = await get_connection(user.id, connection_id)
    if conn is None:
        logfire.error("Connection not found", connection_id=connection_id, user_id=user.id)
        raise HTTPException(status_code=404, detail="Connection not found")

    try:
        server_dsn, db_name = parse_connection_string(conn.connection_string)
        if not db_name:
            db_name = conn.database_name or db_settings.db_name
        if not db_name:
            raise HTTPException(status_code=400, detail="Database name is required")
        logfire.info("Using database", db_name=db_name, user_id=user.id)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid connection string: {str(e)}")

    requested_pipeline_mode = _normalize_pipeline_mode(pipeline_mode)
    if requested_pipeline_mode not in ("pipeline", "single", "versus"):
        requested_pipeline_mode = "pipeline"

    execution = QueryExecution(
        user_id=user.id,
        session_id=session_id,
        connection_id=connection_id,
        user_query=user_query,
        status="pending",
        pipeline_mode=requested_pipeline_mode,
    )
    exec_id = await save_execution(execution)

    if execution.pipeline_mode == "single":
        asyncio.create_task(
            run_single_pipeline_with_updates(
                exec_id=exec_id,
                user_message=user_query,
                server_dsn=server_dsn,
                database=db_name,
                user_id=user.id,
            )
        )
    elif execution.pipeline_mode == "versus":
        pipeline_execution = QueryExecution(
            user_id=user.id,
            session_id=session_id,
            connection_id=connection_id,
            user_query=user_query,
            status="pending",
            pipeline_mode="pipeline",
            parent_execution_id=exec_id,
        )
        single_execution = QueryExecution(
            user_id=user.id,
            session_id=session_id,
            connection_id=connection_id,
            user_query=user_query,
            status="pending",
            pipeline_mode="single",
            parent_execution_id=exec_id,
        )
        pipeline_execution_id = await save_execution(pipeline_execution)
        single_execution_id = await save_execution(single_execution)
        await update_execution_metrics(
            exec_id,
            comparison_execution_ids={"pipeline": pipeline_execution_id, "single": single_execution_id},
            current_activity="Starting versus comparison",
        )
        asyncio.create_task(
            run_versus_pipeline_with_updates(
                exec_id=exec_id,
                pipeline_exec_id=pipeline_execution_id,
                single_exec_id=single_execution_id,
                user_message=user_query,
                server_dsn=server_dsn,
                database=db_name,
                session_id=session_id,
                user_id=user.id,
            )
        )
    else:
        asyncio.create_task(
            run_pipeline_with_updates(
                exec_id=exec_id,
                user_message=user_query,
                server_dsn=server_dsn,
                database=db_name,
                session_id=session_id,
                user_id=user.id,
            )
        )

    return JSONResponse(content={"execution_id": exec_id, "session_id": session_id, "status": "pending"})


@app.get("/api/queries/{execution_id}/status")
async def get_query_status(execution_id: str, user: CurrentUserDep):
    """Get query execution status (for polling)."""
    execution = await get_execution(execution_id, user_id=user.id)
    if execution is None:
        raise HTTPException(status_code=404, detail="Execution not found")
    result = await _serialize_execution_with_comparison(execution)
    return result


@app.get("/api/queries/{execution_id}")
async def get_query(execution_id: str, user: CurrentUserDep):
    """Get full query execution details."""
    execution = await get_execution(execution_id, user_id=user.id)
    if execution is None:
        raise HTTPException(status_code=404, detail="Execution not found")
    return await _serialize_execution_with_comparison(execution)


@app.delete("/api/queries/{execution_id}")
async def delete_query_endpoint(execution_id: str, user: CurrentUserDep):
    """Delete a query execution from its session."""
    execution = await get_execution(execution_id, user_id=user.id)
    if execution is None:
        raise HTTPException(status_code=404, detail="Execution not found")
    if execution.parent_execution_id is not None:
        raise HTTPException(status_code=400, detail="Cannot delete child execution directly")
    deleted = await delete_execution(execution_id, user_id=user.id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Execution not found")
    logfire.info("Deleted query execution", execution_id=execution_id)
    return {"success": True, "session_id": execution.session_id}


@app.get("/api/queries/{execution_id}/feedback")
@app.get("/api/executions/{execution_id}/feedback")
async def get_query_feedback(execution_id: str, user: CurrentUserDep):
    """Get current feedback for a query execution."""
    if not _feedback_enabled():
        return {"feedback_enabled": False, "user_feedback": None}

    execution = await get_execution(execution_id, user_id=user.id)
    if execution is None:
        raise HTTPException(status_code=404, detail="Execution not found")

    if _normalize_pipeline_mode(execution.pipeline_mode) == "versus":
        comparison_ids = execution.comparison_execution_ids or {}
        return {
            "feedback_enabled": True,
            "user_feedback": await get_versus_feedback(
                comparison_ids.get("pipeline", ""), comparison_ids.get("single", "")
            ),
        }

    return {"feedback_enabled": True, "user_feedback": await get_feedback(execution_id)}


@app.post("/api/queries/{execution_id}/feedback")
@app.post("/api/executions/{execution_id}/feedback")
async def save_query_feedback(execution_id: str, payload: FeedbackRequest, user: CurrentUserDep):
    """Save positive or negative feedback for a terminal query execution."""
    if not _feedback_enabled():
        raise HTTPException(status_code=503, detail="Feedback analytics is disabled")

    execution = await get_execution(execution_id, user_id=user.id)
    if execution is None:
        raise HTTPException(status_code=404, detail="Execution not found")

    if execution.status not in {"completed", "error"}:
        raise HTTPException(status_code=409, detail="Feedback can only be saved for terminal executions")

    pipeline_mode = _normalize_pipeline_mode(execution.pipeline_mode)
    if pipeline_mode == "versus":
        if payload.value not in {"pipeline", "single"}:
            raise HTTPException(status_code=400, detail="Versus feedback must be 'pipeline' or 'single'")

        comparison_ids = execution.comparison_execution_ids or {}
        pipeline_execution_id = comparison_ids.get("pipeline")
        single_execution_id = comparison_ids.get("single")
        if not pipeline_execution_id or not single_execution_id:
            raise HTTPException(status_code=409, detail="Versus comparison executions are not available")

        existing_preference = await get_versus_feedback(pipeline_execution_id, single_execution_id)
        if existing_preference is not None:
            raise HTTPException(status_code=409, detail="Feedback has already been submitted")

        pipeline_execution = await get_execution(pipeline_execution_id, user_id=user.id)
        single_execution = await get_execution(single_execution_id, user_id=user.id)
        if pipeline_execution is None or single_execution is None:
            raise HTTPException(status_code=404, detail="Versus comparison execution not found")
        if pipeline_execution.status not in {"completed", "error"} or single_execution.status not in {
            "completed",
            "error",
        }:
            raise HTTPException(status_code=409, detail="Feedback can only be saved after both runs finish")

        preferred: Literal["pipeline", "single"] = "pipeline" if payload.value == "pipeline" else "single"
        snapshots = [
            extract_feedback_snapshot(pipeline_execution, "positive" if preferred == "pipeline" else "negative"),
            extract_feedback_snapshot(single_execution, "positive" if preferred == "single" else "negative"),
        ]
        try:
            for snapshot in snapshots:
                await save_feedback(snapshot)
        except RuntimeError as e:
            raise HTTPException(status_code=503, detail=str(e))
        except Exception as e:
            logfire.warning("Could not save versus feedback", exec_id=execution_id, error=str(e))
            raise HTTPException(status_code=500, detail="Could not save feedback")

        return {"ok": True, "feedback_enabled": True, "user_feedback": preferred}

    if payload.value not in {"positive", "negative"}:
        raise HTTPException(status_code=400, detail="Feedback must be 'positive' or 'negative'")

    existing_feedback = await get_feedback(execution_id)
    if existing_feedback is not None:
        raise HTTPException(status_code=409, detail="Feedback has already been submitted")

    feedback_value: Literal["positive", "negative"] = "positive" if payload.value == "positive" else "negative"
    snapshot = extract_feedback_snapshot(execution, feedback_value)
    try:
        await save_feedback(snapshot)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logfire.warning("Could not save execution feedback", exec_id=execution_id, error=str(e))
        raise HTTPException(status_code=500, detail="Could not save feedback")

    return {"ok": True, "feedback_enabled": True, "user_feedback": payload.value}


@app.get("/api/queries/latest")
async def get_latest_execution(user: CurrentUserDep):
    """Get the latest execution (for restoring state on page load)."""
    executions = await list_executions(user.id, limit=1)
    if not executions:
        return {"execution": None}

    exec = executions[0]
    return {"execution": await _serialize_execution_with_comparison(exec)}


# Pipeline Integration


async def run_single_pipeline_with_updates(
    exec_id: str,
    user_message: str,
    server_dsn: str,
    database: str,
    user_id: str,
):
    """Run single agent pipeline with Redis updates."""
    from app.agents.single_workflow import run_single_agent_pipeline

    started_at = datetime.now()

    def elapsed_ms() -> int:
        return int((datetime.now() - started_at).total_seconds() * 1000)

    try:
        await update_execution_metrics(
            exec_id,
            model_name="openai/gpt-5-mini",
            current_activity="Starting single-agent run",
        )
        sql, _tool_calls, usage, error = await run_single_agent_pipeline(
            user_message=user_message,
            server_dsn=server_dsn,
            database=database,
            execution_id=exec_id,
        )
        if error:
            latency_ms = elapsed_ms()
            await update_execution_status(exec_id, "error", error=error, latency_ms=latency_ms)
            await update_execution_metrics(
                exec_id,
                usage=usage,
                current_activity="Single-agent run failed",
                latency_ms=latency_ms,
            )
            return
        if sql:
            query_result = await execute_query(server_dsn, database, sql)
            has_error = query_result and len(query_result) == 1 and "error" in query_result[0]
            latency_ms = elapsed_ms()
            await update_execution_status(
                exec_id,
                "error" if has_error else "completed",
                sql_query=sql,
                query_result=query_result,
                error=query_result[0]["error"] if has_error else None,
                latency_ms=latency_ms,
            )
            await update_execution_metrics(
                exec_id,
                usage=usage,
                current_activity="Single-agent run completed",
                latency_ms=latency_ms,
            )
        else:
            latency_ms = elapsed_ms()
            await update_execution_status(exec_id, "error", error="No SQL generated", latency_ms=latency_ms)
            await update_execution_metrics(
                exec_id,
                usage=usage,
                current_activity="Single-agent run failed",
                latency_ms=latency_ms,
            )
    except Exception as e:
        from app.agents.llm_timeout import format_model_error

        logfire.error("Single agent pipeline failed", error=str(e), exc_info=True)
        latency_ms = elapsed_ms()
        await update_execution_status(exec_id, "error", error=format_model_error(e), latency_ms=latency_ms)
        await update_execution_metrics(
            exec_id,
            current_activity="Single-agent run failed",
            latency_ms=latency_ms,
        )


async def run_pipeline_with_updates(
    exec_id: str,
    user_message: str,
    server_dsn: str,
    database: str,
    session_id: str,
    user_id: str,
):
    """Run pipeline with Redis state updates at each step."""
    from app.agents.workflow import run_new_pipeline

    try:
        # Update status to running
        await update_execution_status(exec_id, "running")
        await update_execution_metrics(
            exec_id,
            model_name="openai/gpt-5-mini",
            current_activity="Starting multi-agent pipeline",
        )

        # Run the compositor pipeline - it will update Redis at each step
        result = await run_new_pipeline(
            user_message=user_message,
            server_dsn=server_dsn,
            database=database,
            execution_id=exec_id,
            session_id=session_id,
            user_id=user_id,
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
                    latency_ms=result.trace.latency_ms,
                )
                await update_execution_metrics(
                    exec_id, current_activity="Pipeline completed", latency_ms=result.trace.latency_ms
                )
            else:
                # Validator rejected, but SQL exists — only report error if execution fails.
                execution_error = None
                if query_result and len(query_result) == 1 and "error" in query_result[0]:
                    execution_error = query_result[0]["error"]

                if execution_error:
                    await update_execution_status(
                        exec_id,
                        "error",
                        sql_query=sql_query,
                        query_result=query_result,
                        error=execution_error,
                        latency_ms=result.trace.latency_ms,
                    )
                    await update_execution_metrics(
                        exec_id,
                        current_activity="SQL execution failed",
                        latency_ms=result.trace.latency_ms,
                    )
                else:
                    await update_execution_status(
                        exec_id,
                        "completed",
                        sql_query=sql_query,
                        query_result=query_result,
                        latency_ms=result.trace.latency_ms,
                    )
                    await update_execution_metrics(
                        exec_id,
                        current_activity="Pipeline completed (validator had warnings)",
                        latency_ms=result.trace.latency_ms,
                    )
        elif result.status == "ERROR":
            await update_execution_status(
                exec_id, "error", error=result.error or "Unknown error", latency_ms=result.trace.latency_ms
            )
        else:
            await update_execution_status(
                exec_id, "error", error=result.error or "Pipeline failed", latency_ms=result.trace.latency_ms
            )

    except Exception as e:
        from app.agents.llm_timeout import format_model_error

        logfire.error("Pipeline execution failed", error=str(e), exc_info=True)
        await update_execution_status(exec_id, "error", error=format_model_error(e))


async def run_versus_pipeline_with_updates(
    exec_id: str,
    pipeline_exec_id: str,
    single_exec_id: str,
    user_message: str,
    server_dsn: str,
    database: str,
    session_id: str,
    user_id: str,
):
    """Run the multi-agent pipeline and single agent at the same time."""
    started_at = datetime.now()
    try:
        await update_execution_status(exec_id, "running")
        await update_execution_metrics(exec_id, current_activity="Running versus comparison")
        await asyncio.gather(
            run_pipeline_with_updates(
                exec_id=pipeline_exec_id,
                user_message=user_message,
                server_dsn=server_dsn,
                database=database,
                session_id=session_id,
                user_id=user_id,
            ),
            run_single_pipeline_with_updates(
                exec_id=single_exec_id,
                user_message=user_message,
                server_dsn=server_dsn,
                database=database,
                user_id=user_id,
            ),
        )
        pipeline_execution = await get_execution(pipeline_exec_id)
        single_execution = await get_execution(single_exec_id)
        child_statuses = {
            execution.status for execution in (pipeline_execution, single_execution) if execution is not None
        }
        overall_status = "completed" if "completed" in child_statuses else "error"
        error_message = None
        if overall_status == "error":
            error_message = "Both approaches failed." if child_statuses == {"error"} else "One approach failed."
        elapsed_ms = int((datetime.now() - started_at).total_seconds() * 1000)
        await update_execution_status(exec_id, overall_status, error=error_message, latency_ms=elapsed_ms)
        await update_execution_metrics(exec_id, current_activity="Versus comparison completed", latency_ms=elapsed_ms)
    except Exception as e:
        logfire.error("Versus execution failed", error=str(e), exc_info=True)
        await update_execution_status(exec_id, "error", error=str(e))


async def execute_query(server_dsn: str, database: str, sql_query: str) -> list[dict[str, Any]]:
    """Execute SQL query and return results as list of dicts.

    Uses the dialect-aware adapter so the same path serves PostgreSQL and MySQL.
    """
    from app.agents.tools import clean_sql
    from app.db.connection import database_connect

    try:
        sql_clean = clean_sql(sql_query)

        if not database:
            logfire.error("Database name is empty", server_dsn=server_dsn)
            return [{"error": "Database name is required"}]

        logfire.info(
            "Executing query",
            server_dsn=server_dsn[:50] + "...",
            database=database,
            sql_preview=sql_clean[:100],
        )

        async with database_connect(server_dsn=server_dsn, database=database) as conn:
            success, rows, error = await conn.execute_sql_safe(sql_clean, limit=1000)
            if not success:
                logfire.error("Database error", error=error, sql_preview=sql_query[:200])
                return [{"error": error or "Unknown database error"}]
            logfire.info("Query executed successfully", row_count=len(rows or []))
            return rows or []
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
    dataset: Annotated[str, Form()],
    indices: Annotated[str, Form()],  # Comma-separated or range string like "1,2,3" or "1-5"
    connection_id: Annotated[str, Form()],
    metrics: Annotated[str, Form()] = "em,exa",  # Comma-separated: "em", "exa", or "em,exa"
    timeout_s: Annotated[int, Form()] = 30,
    max_concurrent: Annotated[int, Form()] = 5,
):
    """Run benchmark on selected questions."""
    from app.auth.dependencies import get_optional_user
    from app.benchmarks.datasets import load_bird, load_livraria, parse_indices
    from app.benchmarks.runner import run_benchmark

    # Validate dataset
    if dataset not in ("bird", "livraria"):
        raise HTTPException(status_code=400, detail=f"Invalid dataset: {dataset}")

    user = await get_optional_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required")

    conn = await get_connection(user.id, connection_id)
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
        logfire.info("Using database for benchmark", db_name=db_name, user_id=user.id)
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

        from app.benchmarks.errors import is_blocking_benchmark_error

        errors = sum(
            1
            for r in results
            if is_blocking_benchmark_error(
                error=r.get("error"),
                execution_error=r.get("execution_error"),
                predicted_sql=r.get("predicted_sql") or "",
            )
        )

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
