from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

import logfire
from pydantic import BaseModel, Field

from app.prompts import AGENT_IDS
from app.redis_client import get_redis_client

PROMPTS_CONFIG_KEY = "prompts:config"


class ConnectionString(BaseModel):
    """SQL database connection string model (PostgreSQL or MySQL)."""

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
    mapper_output: dict[str, Any] | str | None = None  # MapperOutput as dict; legacy runs may be strings
    generator_output: dict[str, Any] | None = None  # GeneratorOutput as dict
    validator_output: dict[str, Any] | None = None  # ValidatorOutput as dict
    analyzer_output: dict[str, Any] | None = None  # AnalyzerOutput as dict
    single_agent_tool_calls: list[dict[str, Any]] = Field(default_factory=list)  # Tool call timeline for single agent
    pipeline_tool_calls: list[dict[str, Any]] = Field(default_factory=list)  # Tool call timeline for multi-agent pipeline
    usage: dict[str, Any] | None = None
    model_name: str | None = None
    current_activity: str | None = None
    latency_ms: int | None = None
    pipeline_mode: str = "pipeline"  # "pipeline" | "single" | "versus"
    parent_execution_id: str | None = None
    comparison_execution_ids: dict[str, str] | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


def _execution_created_at(execution: QueryExecution) -> datetime:
    return execution.created_at


def _session_updated_at(session: Session) -> datetime:
    return session.updated_at


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
    _ = await cast(Any, redis_client).sadd("connections:list", conn.id)
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
    conn_ids_set = cast(set[str], await cast(Any, redis_client).smembers("connections:list"))
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
    deleted_count = cast(int, await redis_client.delete(key))
    _ = await cast(Any, redis_client).srem("connections:list", conn_id)
    return deleted_count > 0


async def _delete_execution_record(exec_id: str) -> bool:
    """Delete an execution record from Redis without touching child/parent relationships."""
    redis_client = await get_redis_client()
    key = f"execution:{exec_id}"
    deleted_count = cast(int, await redis_client.delete(key))
    _ = await cast(Any, redis_client).srem("executions:list", exec_id)
    return deleted_count > 0


async def _remove_execution_from_session(session_id: str, execution_id: str) -> None:
    """Remove an execution ID from its session list."""
    session = await get_session(session_id)
    if session is None or execution_id not in session.execution_ids:
        return
    session.execution_ids.remove(execution_id)
    session.updated_at = datetime.now(UTC)
    await save_session(session)


async def delete_execution(exec_id: str) -> bool:
    """Delete a query execution and any versus child executions."""
    execution = await get_execution(exec_id)
    if execution is None:
        return False

    if execution.comparison_execution_ids:
        for child_id in execution.comparison_execution_ids.values():
            child = await get_execution(child_id)
            if child is not None:
                await _delete_execution_record(child_id)
                await _remove_execution_from_session(child.session_id, child_id)

    session_id = execution.session_id
    deleted = await _delete_execution_record(exec_id)
    await _remove_execution_from_session(session_id, exec_id)
    return deleted


async def delete_session(session_id: str) -> bool:
    """Delete a session and all of its executions."""
    session = await get_session(session_id)
    if session is None:
        return False

    for exec_id in list(session.execution_ids):
        execution = await get_execution(exec_id)
        if execution is None:
            continue
        if execution.parent_execution_id is not None:
            continue
        await delete_execution(exec_id)

    for exec_id in list(session.execution_ids):
        if await get_execution(exec_id) is not None:
            await _delete_execution_record(exec_id)

    redis_client = await get_redis_client()
    key = f"session:{session_id}"
    deleted_count = cast(int, await redis_client.delete(key))
    _ = await cast(Any, redis_client).srem("sessions:list", session_id)
    return deleted_count > 0


# Query Execution Operations


async def save_execution(exec: QueryExecution) -> str:
    """Save query execution to Redis."""
    redis_client = await get_redis_client()
    key = f"execution:{exec.id}"
    exec.updated_at = datetime.now(UTC)
    await redis_client.set(key, exec.model_dump_json(), ex=86400)  # 24 hour TTL
    _ = await cast(Any, redis_client).sadd("executions:list", exec.id)
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
        elif step == "mapper":
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
    latency_ms: int | None = None,
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
    if latency_ms is not None:
        exec.latency_ms = latency_ms
    exec.updated_at = datetime.now(UTC)
    await save_execution(exec)
    logfire.info(
        "Updated execution status",
        extra={"exec_id": exec_id, "status": status, "error": error, "has_analyzer": analyzer_output is not None},
    )


async def append_single_agent_tool_call(exec_id: str, tool_call: dict[str, Any]) -> None:
    """Append a tool call to the single agent timeline (for real-time UI updates)."""
    exec_obj = await get_execution(exec_id)
    if exec_obj is None:
        return
    exec_obj.single_agent_tool_calls.append(tool_call)
    exec_obj.updated_at = datetime.now(UTC)
    await save_execution(exec_obj)


async def append_pipeline_tool_call(exec_id: str, tool_call: dict[str, Any]) -> None:
    """Append a tool call to the multi-agent pipeline timeline."""
    exec_obj = await get_execution(exec_id)
    if exec_obj is None:
        return
    exec_obj.pipeline_tool_calls.append(tool_call)
    exec_obj.updated_at = datetime.now(UTC)
    await save_execution(exec_obj)


async def update_execution_metrics(
    exec_id: str,
    *,
    usage: dict[str, Any] | None = None,
    model_name: str | None = None,
    current_activity: str | None = None,
    latency_ms: int | None = None,
    comparison_execution_ids: dict[str, str] | None = None,
) -> None:
    """Update execution telemetry fields without changing the main status."""
    exec_obj = await get_execution(exec_id)
    if exec_obj is None:
        return
    if usage is not None:
        exec_obj.usage = usage
    if model_name is not None:
        exec_obj.model_name = model_name
    if current_activity is not None:
        exec_obj.current_activity = current_activity
    if latency_ms is not None:
        exec_obj.latency_ms = latency_ms
    if comparison_execution_ids is not None:
        exec_obj.comparison_execution_ids = comparison_execution_ids
    exec_obj.updated_at = datetime.now(UTC)
    await save_execution(exec_obj)


async def list_executions(limit: int = 100) -> list[QueryExecution]:
    """List recent query executions from Redis."""
    redis_client = await get_redis_client()
    exec_ids_set = cast(set[str], await cast(Any, redis_client).smembers("executions:list"))
    exec_ids = list(exec_ids_set)[:limit] if exec_ids_set else []
    executions = []
    for exec_id in exec_ids:
        exec = await get_execution(exec_id)
        if exec and exec.parent_execution_id is None:
            executions.append(exec)
    # Sort by created_at descending
    executions.sort(key=_execution_created_at, reverse=True)
    return executions


async def list_executions_by_session(session_id: str) -> list[QueryExecution]:
    """List all executions for a specific session."""
    session = await get_session(session_id)
    if session is None:
        return []
    executions = []
    for exec_id in session.execution_ids:
        exec = await get_execution(exec_id)
        if exec and exec.parent_execution_id is None:
            executions.append(exec)
    # Sort by created_at descending
    executions.sort(key=_execution_created_at, reverse=True)
    return executions


# Session Operations


async def save_session(session: Session) -> str:
    """Save session to Redis."""
    redis_client = await get_redis_client()
    key = f"session:{session.id}"
    session.updated_at = datetime.now(UTC)
    await redis_client.set(key, session.model_dump_json(), ex=86400 * 7)  # 7 day TTL
    _ = await cast(Any, redis_client).sadd("sessions:list", session.id)
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
    session_ids_set = cast(set[str], await cast(Any, redis_client).smembers("sessions:list"))
    session_ids = list(session_ids_set) if session_ids_set else []
    sessions = []
    for session_id in session_ids:
        session = await get_session(session_id)
        if session:
            sessions.append(session)
    # Sort by updated_at descending (most recent first)
    sessions.sort(key=_session_updated_at, reverse=True)
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


# Prompt Config Operations


async def get_prompt_config() -> dict[str, str]:
    """Get custom prompt overrides from Redis. Returns only non-empty custom prompts."""
    redis_client = await get_redis_client()
    data = await redis_client.get(PROMPTS_CONFIG_KEY)
    if data is None:
        return {}
    try:
        config = json.loads(data)
        if not isinstance(config, dict):
            return {}
        # Return only non-empty prompts for valid agent IDs
        return {
            agent_id: prompt
            for agent_id, prompt in config.items()
            if agent_id in AGENT_IDS and prompt and isinstance(prompt, str)
        }
    except (json.JSONDecodeError, TypeError):
        return {}


async def save_prompt_config(agent_id: str, prompt: str) -> None:
    """Save a custom prompt for an agent."""
    if agent_id not in AGENT_IDS:
        raise ValueError(f"Invalid agent_id: {agent_id}. Valid: {AGENT_IDS}")
    redis_client = await get_redis_client()
    config = {}
    data = await redis_client.get(PROMPTS_CONFIG_KEY)
    if data:
        try:
            config = json.loads(data)
            if not isinstance(config, dict):
                config = {}
        except (json.JSONDecodeError, TypeError):
            config = {}
    config[agent_id] = prompt
    await redis_client.set(PROMPTS_CONFIG_KEY, json.dumps(config), ex=86400 * 365)  # 1 year TTL
    logfire.info("Saved prompt config", extra={"agent_id": agent_id})


async def reset_prompt_config(agent_id: str) -> None:
    """Reset an agent's prompt to default (remove custom override)."""
    if agent_id not in AGENT_IDS:
        raise ValueError(f"Invalid agent_id: {agent_id}. Valid: {AGENT_IDS}")
    redis_client = await get_redis_client()
    config = {}
    data = await redis_client.get(PROMPTS_CONFIG_KEY)
    if data:
        try:
            config = json.loads(data)
            if not isinstance(config, dict):
                config = {}
        except (json.JSONDecodeError, TypeError):
            config = {}
    config.pop(agent_id, None)
    if config:
        await redis_client.set(PROMPTS_CONFIG_KEY, json.dumps(config), ex=86400 * 365)
    else:
        await redis_client.delete(PROMPTS_CONFIG_KEY)
    logfire.info("Reset prompt config", extra={"agent_id": agent_id})
