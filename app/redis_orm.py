from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import logfire
from pydantic import BaseModel, Field

from app.redis_client import get_redis_client


class ConnectionString(BaseModel):
    """PostgreSQL connection string model."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    connection_string: str
    database_name: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Session(BaseModel):
    """Session model for grouping query executions."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str | None = None  # Optional session name
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    execution_ids: list[str] = Field(default_factory=list)  # List of execution IDs in this session


class QueryExecution(BaseModel):
    """Query execution state model."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    session_id: str  # Session this execution belongs to
    connection_id: str
    user_query: str
    status: str = "pending"  # "pending" | "running" | "completed" | "error"
    current_step: str | None = None  # "interpreter" | "mapper" | "generator" | "validator"
    step_status: dict[str, str] = Field(default_factory=dict)  # {"interpreter": "done", ...}
    sql_query: str | None = None
    query_result: list[dict[str, Any]] | None = None
    error: str | None = None
    # Agent outputs
    interpreter_output: dict[str, Any] | None = None  # InterpreterOutput as dict
    mapper_output: str | None = None  # mapperOutput is a string
    generator_output: dict[str, Any] | None = None  # GeneratorOutput as dict
    validator_output: dict[str, Any] | None = None  # ValidatorOutput as dict
    analyzer_output: dict[str, Any] | None = None  # AnalyzerOutput as dict
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# Connection String Operations


async def save_connection_string(conn: ConnectionString) -> str:
    """Save connection string to Redis."""
    logfire.info(
        f"Saving connection string",
        extra={"conn_id": conn.id, "database": conn.database_name},
    )
    redis_client = await get_redis_client()
    key = f"connection:{conn.id}"
    json_data = conn.model_dump_json()
    logfire.info(
        f"Saving JSON data to Redis",
        extra={"json_length": len(json_data), "key": key},
    )
    await redis_client.set(key, json_data)
    _ = await redis_client.sadd("connections:list", conn.id)  # type: ignore
    logfire.info(f"Successfully saved connection to Redis", extra={"key": key})
    return conn.id


async def get_connection_string(conn_id: str) -> ConnectionString | None:
    """Get connection string from Redis."""
    redis_client = await get_redis_client()
    key = f"connection:{conn_id}"
    data = await redis_client.get(key)
    if data is None:
        return None
    return ConnectionString.model_validate_json(data)


async def list_connection_strings() -> list[ConnectionString]:
    """List all connection strings from Redis."""
    redis_client = await get_redis_client()
    conn_ids_set: set[str] = await redis_client.smembers("connections:list")  # type: ignore
    conn_ids = list(conn_ids_set) if conn_ids_set else []
    connections = []
    for conn_id in conn_ids:
        conn = await get_connection_string(conn_id)
        if conn:
            connections.append(conn)
    return connections


async def delete_connection_string(conn_id: str) -> bool:
    """Delete connection string from Redis."""
    redis_client = await get_redis_client()
    key = f"connection:{conn_id}"
    deleted_count: int = await redis_client.delete(key)  # type: ignore
    _ = await redis_client.srem("connections:list", conn_id)  # type: ignore
    return deleted_count > 0


# Query Execution Operations


async def save_execution(exec: QueryExecution) -> str:
    """Save query execution to Redis."""
    redis_client = await get_redis_client()
    key = f"execution:{exec.id}"
    exec.updated_at = datetime.now(UTC)
    await redis_client.set(key, exec.model_dump_json(), ex=86400)  # 24 hour TTL
    _ = await redis_client.sadd("executions:list", exec.id)  # type: ignore
    # Add execution to session
    await add_execution_to_session(exec.session_id, exec.id)
    logfire.info("Saved QueryExecution to Redis", extra={"exec_id": exec.id, "session_id": exec.session_id})
    return exec.id


async def get_execution(exec_id: str) -> QueryExecution | None:
    """Get query execution from Redis."""
    redis_client = await get_redis_client()
    key = f"execution:{exec_id}"
    data = await redis_client.get(key)
    if data is None:
        return None
    return QueryExecution.model_validate_json(data)


async def update_execution_step(
    exec_id: str,
    step: str,
    status: str,
    output: dict[str, Any] | str | None = None,
) -> None:
    """Update execution step status and optionally save output."""
    exec = await get_execution(exec_id)
    if exec is None:
        return
    exec.current_step = step if status == "running" else exec.current_step
    exec.step_status[step] = status

    # Save agent output when step is done
    if status == "done" and output is not None:
        if step == "interpreter" and isinstance(output, dict):
            exec.interpreter_output = output
        elif step == "mapper" and isinstance(output, str):
            exec.mapper_output = output
        elif step == "generator" and isinstance(output, dict):
            exec.generator_output = output
        elif step == "validator" and isinstance(output, dict):
            exec.validator_output = output

    exec.updated_at = datetime.now(UTC)
    await save_execution(exec)
    logfire.info(
        "Updated execution step",
        extra={"exec_id": exec_id, "step": step, "status": status, "has_output": output is not None},
    )


async def update_execution_status(
    exec_id: str,
    status: str,
    sql_query: str | None = None,
    query_result: list[dict[str, Any]] | None = None,
    error: str | None = None,
    analyzer_output: dict[str, Any] | None = None,
) -> None:
    """Update execution status and results."""
    exec = await get_execution(exec_id)
    if exec is None:
        return
    exec.status = status
    if sql_query is not None:
        exec.sql_query = sql_query
    if query_result is not None:
        exec.query_result = query_result
    if error is not None:
        exec.error = error
    if analyzer_output is not None:
        exec.analyzer_output = analyzer_output
    exec.updated_at = datetime.now(UTC)
    await save_execution(exec)
    logfire.info(
        "Updated execution status",
        extra={"exec_id": exec_id, "status": status, "error": error, "has_analyzer": analyzer_output is not None},
    )


async def list_executions(limit: int = 100) -> list[QueryExecution]:
    """List recent query executions from Redis."""
    redis_client = await get_redis_client()
    exec_ids_set: set[str] = await redis_client.smembers("executions:list")
    exec_ids = list(exec_ids_set)[:limit] if exec_ids_set else []
    executions = []
    for exec_id in exec_ids:
        exec = await get_execution(exec_id)
        if exec:
            executions.append(exec)
    # Sort by created_at descending
    executions.sort(key=lambda x: x.created_at, reverse=True)
    return executions


async def list_executions_by_session(session_id: str) -> list[QueryExecution]:
    """List all executions for a specific session."""
    session = await get_session(session_id)
    if session is None:
        return []
    executions = []
    for exec_id in session.execution_ids:
        exec = await get_execution(exec_id)
        if exec:
            executions.append(exec)
    # Sort by created_at descending
    executions.sort(key=lambda x: x.created_at, reverse=True)
    return executions


# Session Operations


async def save_session(session: Session) -> str:
    """Save session to Redis."""
    redis_client = await get_redis_client()
    key = f"session:{session.id}"
    session.updated_at = datetime.now(UTC)
    await redis_client.set(key, session.model_dump_json(), ex=86400 * 7)  # 7 day TTL
    _ = await redis_client.sadd("sessions:list", session.id)  # type: ignore
    logfire.info("Saved Session to Redis", extra={"session_id": session.id})
    return session.id


async def get_session(session_id: str) -> Session | None:
    """Get session from Redis."""
    redis_client = await get_redis_client()
    key = f"session:{session_id}"
    data = await redis_client.get(key)
    if data is None:
        return None
    return Session.model_validate_json(data)


async def list_sessions() -> list[Session]:
    """List all sessions from Redis."""
    redis_client = await get_redis_client()
    session_ids_set: set[str] = await redis_client.smembers("sessions:list")  # type: ignore
    session_ids = list(session_ids_set) if session_ids_set else []
    sessions = []
    for session_id in session_ids:
        session = await get_session(session_id)
        if session:
            sessions.append(session)
    # Sort by updated_at descending (most recent first)
    sessions.sort(key=lambda x: x.updated_at, reverse=True)
    return sessions


async def add_execution_to_session(session_id: str, execution_id: str) -> None:
    """Add an execution to a session."""
    session = await get_session(session_id)
    if session is None:
        return
    if execution_id not in session.execution_ids:
        session.execution_ids.append(execution_id)
        session.updated_at = datetime.now(UTC)
        await save_session(session)
        logfire.info(
            "Added execution to session",
            extra={"session_id": session_id, "execution_id": execution_id},
        )
