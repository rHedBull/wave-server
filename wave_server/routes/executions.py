from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from wave_server.db import get_db
from wave_server.models import Command, Event, Execution, Sequence
from wave_server.schemas import (
    CommandResolve,
    CommandResponse,
    EventResponse,
    ExecutionCreate,
    ExecutionResponse,
)
from wave_server import storage

router = APIRouter()


@router.post(
    "/sequences/{sequence_id}/executions",
    response_model=ExecutionResponse,
    status_code=201,
)
async def create_execution(
    sequence_id: str, body: ExecutionCreate, db: AsyncSession = Depends(get_db)
):
    seq = await db.get(Sequence, sequence_id)
    if not seq:
        raise HTTPException(404, "Sequence not found")
    import json

    config = json.dumps(
        {
            "concurrency": body.concurrency,
            "timeout_ms": body.timeout_ms,
        }
    )
    execution = Execution(
        sequence_id=sequence_id,
        runtime=body.runtime or "claude",
        config=config,
    )
    db.add(execution)
    await db.commit()
    await db.refresh(execution)
    # TODO: launch background execution task (Phase 2)
    return execution


@router.get(
    "/sequences/{sequence_id}/executions",
    response_model=list[ExecutionResponse],
)
async def list_executions(
    sequence_id: str, db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(Execution)
        .where(Execution.sequence_id == sequence_id)
        .order_by(Execution.created_at.desc())
    )
    return result.scalars().all()


@router.get("/executions/{execution_id}", response_model=ExecutionResponse)
async def get_execution(execution_id: str, db: AsyncSession = Depends(get_db)):
    exc = await db.get(Execution, execution_id)
    if not exc:
        raise HTTPException(404, "Execution not found")
    return exc


@router.post("/executions/{execution_id}/cancel", status_code=204)
async def cancel_execution(
    execution_id: str, db: AsyncSession = Depends(get_db)
):
    exc = await db.get(Execution, execution_id)
    if not exc:
        raise HTTPException(404, "Execution not found")
    if exc.status not in ("queued", "running"):
        raise HTTPException(400, "Execution is not running")
    exc.status = "cancelled"
    exc.finished_at = datetime.now(timezone.utc)
    await db.commit()


@router.post(
    "/executions/{execution_id}/continue",
    response_model=ExecutionResponse,
    status_code=201,
)
async def continue_execution(
    execution_id: str, db: AsyncSession = Depends(get_db)
):
    exc = await db.get(Execution, execution_id)
    if not exc:
        raise HTTPException(404, "Execution not found")
    if exc.status not in ("failed", "cancelled"):
        raise HTTPException(400, "Execution is not in a resumable state")
    new_exec = Execution(
        sequence_id=exc.sequence_id,
        trigger="continuation",
        runtime=exc.runtime,
        config=exc.config,
    )
    db.add(new_exec)
    await db.commit()
    await db.refresh(new_exec)
    # TODO: launch background continuation task (Phase 2)
    return new_exec


# --- Events ---


@router.get(
    "/executions/{execution_id}/events", response_model=list[EventResponse]
)
async def list_events(
    execution_id: str,
    since: datetime | None = Query(None),
    limit: int = Query(50, le=200),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Event).where(Event.execution_id == execution_id)
    if since:
        stmt = stmt.where(Event.created_at > since)
    stmt = stmt.order_by(Event.created_at).limit(limit)
    result = await db.execute(stmt)
    return result.scalars().all()


# --- Task summary ---


@router.get("/executions/{execution_id}/tasks")
async def list_tasks(execution_id: str, db: AsyncSession = Depends(get_db)):
    exc = await db.get(Execution, execution_id)
    if not exc:
        raise HTTPException(404, "Execution not found")
    # Build task summary from events
    result = await db.execute(
        select(Event)
        .where(Event.execution_id == execution_id)
        .where(Event.event_type.in_(["task_started", "task_completed", "task_failed", "task_skipped"]))
        .order_by(Event.created_at)
    )
    events = result.scalars().all()
    import json

    tasks: dict[str, dict] = {}
    for event in events:
        payload = json.loads(event.payload)
        tid = event.task_id or payload.get("task_id", "")
        if tid not in tasks:
            tasks[tid] = {"task_id": tid, "status": "pending", "phase": event.phase}
        if event.event_type == "task_started":
            tasks[tid]["status"] = "running"
            tasks[tid].update({k: v for k, v in payload.items() if k != "task_id"})
        elif event.event_type == "task_completed":
            tasks[tid]["status"] = "completed"
            tasks[tid].update({k: v for k, v in payload.items() if k != "task_id"})
        elif event.event_type == "task_failed":
            tasks[tid]["status"] = "failed"
            tasks[tid].update({k: v for k, v in payload.items() if k != "task_id"})
        elif event.event_type == "task_skipped":
            tasks[tid]["status"] = "skipped"
    return list(tasks.values())


# --- Task output ---


@router.get("/executions/{execution_id}/output/{task_id}")
async def get_task_output(
    execution_id: str, task_id: str, db: AsyncSession = Depends(get_db)
):
    exc = await db.get(Execution, execution_id)
    if not exc:
        raise HTTPException(404, "Execution not found")
    from fastapi.responses import PlainTextResponse

    content = storage.read_output(execution_id, task_id)
    if content is None:
        raise HTTPException(404, "Output not found")
    return PlainTextResponse(content)


# --- Log ---


@router.get("/executions/{execution_id}/log")
async def get_log(execution_id: str, db: AsyncSession = Depends(get_db)):
    exc = await db.get(Execution, execution_id)
    if not exc:
        raise HTTPException(404, "Execution not found")
    from fastapi.responses import PlainTextResponse

    content = storage.read_log(execution_id)
    if content is None:
        raise HTTPException(404, "Log not found")
    return PlainTextResponse(content)


# --- Blockers ---


@router.get(
    "/executions/{execution_id}/blockers",
    response_model=list[CommandResponse],
)
async def list_blockers(
    execution_id: str, db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(Command)
        .where(Command.execution_id == execution_id)
        .where(Command.picked_up == False)  # noqa: E712
        .order_by(Command.created_at)
    )
    return result.scalars().all()


@router.post(
    "/executions/{execution_id}/blockers/{command_id}",
    response_model=CommandResponse,
)
async def resolve_blocker(
    execution_id: str,
    command_id: str,
    body: CommandResolve,
    db: AsyncSession = Depends(get_db),
):
    cmd = await db.get(Command, command_id)
    if not cmd or cmd.execution_id != execution_id:
        raise HTTPException(404, "Command not found")
    if cmd.action is not None:
        raise HTTPException(400, "Command already resolved")
    cmd.action = body.action
    cmd.message = body.message
    cmd.resolved_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(cmd)
    return cmd
