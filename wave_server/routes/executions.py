import shutil
import socket
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from wave_server.db import get_db
from wave_server.engine.dag import validate_plan
from wave_server.engine.plan_parser import parse_plan
from wave_server.models import Command, Event, Execution, ProjectRepository, Sequence
from wave_server.schemas import (
    CommandResolve,
    CommandResponse,
    EventResponse,
    ExecutionCreate,
    ExecutionResponse,
)
from wave_server import storage

router = APIRouter()


def _check_network() -> bool:
    """Return True if api.anthropic.com:443 is reachable within 5 s."""
    try:
        with socket.create_connection(("api.anthropic.com", 443), timeout=5):
            return True
    except OSError:
        return False


async def _preflight(sequence_id: str, project_id: str, db: AsyncSession) -> None:
    """Raise HTTP 422 if the execution cannot proceed due to missing config."""
    # Plan must exist and be valid
    plan_content = storage.read_plan(sequence_id)
    if not plan_content:
        raise HTTPException(422, "No plan found for this sequence. Upload one first.")
    plan = parse_plan(plan_content)
    valid, errors = validate_plan(plan)
    if not valid:
        raise HTTPException(422, f"Plan validation failed: {'; '.join(errors)}")

    # Repository must be configured and still exist on disk
    repo_result = await db.execute(
        select(ProjectRepository)
        .where(ProjectRepository.project_id == project_id)
        .limit(1)
    )
    repo = repo_result.scalar_one_or_none()
    if not repo:
        raise HTTPException(
            422,
            "No repository configured for this project. "
            "Go to Project Settings and add a repository path first.",
        )
    if not Path(repo.path).is_dir():
        raise HTTPException(
            422,
            f"Repository path does not exist or is not accessible: {repo.path}",
        )

    # Claude CLI must be installed
    if not shutil.which("claude"):
        raise HTTPException(
            422,
            "The 'claude' CLI is not installed or not in PATH. "
            "Install it from https://docs.anthropic.com/en/docs/claude-code",
        )

    # Network must be reachable (runs in threadpool to avoid blocking the event loop)
    import asyncio
    reachable = await asyncio.get_event_loop().run_in_executor(None, _check_network)
    if not reachable:
        raise HTTPException(
            422,
            "Cannot reach api.anthropic.com — check your internet connection or firewall.",
        )


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
    await _preflight(sequence_id, seq.project_id, db)
    import json

    config = json.dumps(
        {
            "concurrency": body.concurrency,
            "timeout_ms": body.timeout_ms,
            "model": body.model,
            "agent_models": body.agent_models,
        }
    )
    execution = Execution(
        sequence_id=sequence_id,
        runtime=body.runtime or "claude",
        config=config,
        source_branch=body.source_branch,
        source_sha=body.source_sha,
    )
    db.add(execution)
    await db.commit()
    await db.refresh(execution)
    # Launch background execution
    from wave_server.engine.execution_manager import launch_execution
    await launch_execution(execution.id, sequence_id)
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
    if exc.status not in ("pending", "running"):
        raise HTTPException(400, "Execution is not running")
    from wave_server.engine.execution_manager import cancel_execution as cancel_bg
    cancel_bg(execution_id)
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
    seq = await db.get(Sequence, exc.sequence_id)
    if seq:
        await _preflight(exc.sequence_id, seq.project_id, db)
    new_exec = Execution(
        sequence_id=exc.sequence_id,
        trigger="continuation",
        runtime=exc.runtime,
        config=exc.config,
        source_branch=exc.source_branch,
        source_sha=exc.source_sha,
    )
    db.add(new_exec)
    await db.commit()
    await db.refresh(new_exec)
    from wave_server.engine.execution_manager import launch_execution
    await launch_execution(new_exec.id, exc.sequence_id)
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
    # Enrich with file existence flags
    for t in tasks.values():
        tid = t["task_id"]
        t["has_output"] = storage.has_output(execution_id, tid)
        t["has_transcript"] = storage.has_transcript(execution_id, tid)
        t["has_task_log"] = storage.has_task_log(execution_id, tid)
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


# --- Transcript ---


@router.get("/executions/{execution_id}/transcript/{task_id}")
async def get_task_transcript(
    execution_id: str, task_id: str, db: AsyncSession = Depends(get_db)
):
    exc = await db.get(Execution, execution_id)
    if not exc:
        raise HTTPException(404, "Execution not found")
    from fastapi.responses import PlainTextResponse

    content = storage.read_transcript(execution_id, task_id)
    if content is None:
        raise HTTPException(404, "Transcript not found")
    return PlainTextResponse(content)


# --- Task Logs (human-readable) ---


@router.get("/executions/{execution_id}/task-logs")
async def list_task_logs(
    execution_id: str, db: AsyncSession = Depends(get_db)
):
    exc = await db.get(Execution, execution_id)
    if not exc:
        raise HTTPException(404, "Execution not found")
    return storage.list_task_logs(execution_id)


@router.get("/executions/{execution_id}/task-logs/search")
async def search_task_logs(
    execution_id: str,
    q: str = Query(..., min_length=1, description="Search query"),
    agent: str = Query("", description="Filter by agent: worker, test-writer, wave-verifier"),
    db: AsyncSession = Depends(get_db),
):
    exc = await db.get(Execution, execution_id)
    if not exc:
        raise HTTPException(404, "Execution not found")
    results = storage.search_task_logs(execution_id, q, agent=agent)
    return {
        "query": q,
        "agent_filter": agent or None,
        "total_files": len(results),
        "total_matches": sum(r["match_count"] for r in results),
        "results": results,
    }


@router.get("/executions/{execution_id}/task-logs/{task_id}")
async def get_task_log(
    execution_id: str, task_id: str, db: AsyncSession = Depends(get_db)
):
    exc = await db.get(Execution, execution_id)
    if not exc:
        raise HTTPException(404, "Execution not found")
    from fastapi.responses import PlainTextResponse

    content = storage.read_task_log(execution_id, task_id)
    if content is None:
        raise HTTPException(404, "Task log not found")
    return PlainTextResponse(content, media_type="text/markdown")


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
