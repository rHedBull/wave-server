import json
import shutil
import socket
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from wave_server.config import settings
from wave_server.db import get_db
from wave_server.engine.dag import validate_plan
from wave_server.engine.plan_parser import parse_plan
from wave_server.models import (
    Command,
    Event,
    Execution,
    Project,
    ProjectRepository,
    Sequence,
)
from wave_server.schemas import (
    CommandResolve,
    CommandResponse,
    EventResponse,
    ExecutionCreate,
    ExecutionResponse,
    PromoteRequest,
    PromoteResponse,
    RerunRequest,
    StandalonePromoteRequest,
)
from wave_server import storage
from wave_server.engine.github_pr import promote_pr

router = APIRouter()


def _check_network() -> bool:
    """Return True if api.anthropic.com:443 is reachable within 5 s."""
    try:
        with socket.create_connection(("api.anthropic.com", 443), timeout=5):
            return True
    except OSError:
        return False


async def _github_api_get(
    path: str,
    github_token: str,
    timeout: int = 10,
) -> tuple[int, dict | None] | None:
    """Perform an authenticated GET against the GitHub API.

    Returns ``(status_code, json_body)`` or ``None`` on network / timeout errors.
    """
    import httpx

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(
                f"https://api.github.com{path}",
                headers={
                    "Authorization": f"token {github_token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
            try:
                body = resp.json()
            except Exception:
                body = None
            return resp.status_code, body
    except Exception:
        return None


async def _check_repo_accessible(
    repo_url: str,
    github_token: str,
) -> tuple[bool | None, str]:
    """Check if the bot account can access the repository and has push permission.

    Queries ``GET /repos/{owner}/{repo}`` and inspects the ``permissions``
    field to verify the bot account has write access.

    Returns ``(accessible, detail)`` where *detail* is a human-readable
    explanation when access is denied or insufficient.
    """
    from wave_server.engine.repo_cache import _cache_key_from_url

    owner_repo = _cache_key_from_url(repo_url)
    if not owner_repo:
        return None, ""

    result = await _github_api_get(f"/repos/{owner_repo}", github_token)
    if result is None:
        return None, ""
    status, body = result
    if status == 200:
        # Check push permission
        permissions = (body or {}).get("permissions", {})
        if not permissions.get("push"):
            return False, (
                f"The bot account can see '{owner_repo}' but does not have push access. "
                f"Add the bot account as a collaborator with write permissions."
            )
        return True, ""
    if status == 404:
        return False, (
            f"Cannot access repository '{owner_repo}'. "
            f"Either the URL is wrong or the bot account does not have access. "
            f"Add the bot account as a collaborator on the repository."
        )
    if status == 401:
        return False, (
            "GitHub token is invalid or expired. "
            "Check the WAVE_GITHUB_TOKEN configuration."
        )
    return None, ""


async def _check_remote_branch_exists(
    repo_url: str,
    branch: str,
    github_token: str,
) -> bool | None:
    """Check if *branch* exists on a GitHub remote repository.

    Returns ``True`` if the branch is found, ``False`` if it is definitely
    missing, or ``None`` if the check could not be performed (URL parse
    error, auth issue, network failure).
    """
    from wave_server.engine.repo_cache import _cache_key_from_url

    owner_repo = _cache_key_from_url(repo_url)
    if not owner_repo:
        return None

    result = await _github_api_get(
        f"/repos/{owner_repo}/branches/{branch}",
        github_token,
    )
    if result is None:
        return None
    status, _ = result
    if status == 200:
        return True
    if status == 404:
        return False
    return None


async def _preflight(sequence_id: str, project_id: str, db: AsyncSession) -> None:
    """Raise HTTP 422 if the execution cannot proceed due to missing config."""
    # Plan must exist and be valid
    plan_content = storage.read_plan(sequence_id)
    if not plan_content:
        raise HTTPException(422, "No plan found for this sequence. Upload one first.")
    try:
        plan = parse_plan(plan_content)
    except ValueError as e:
        raise HTTPException(422, str(e))
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
    from wave_server.engine.repo_cache import is_repo_url

    if not is_repo_url(repo.path) and not Path(repo.path).is_dir():
        raise HTTPException(
            422,
            f"Repository path does not exist or is not accessible: {repo.path}",
        )

    # For URL repos: resolve GitHub auth and verify the token can access the repo
    project = await db.get(Project, project_id)
    project_env: dict[str, str] = {}
    if project and project.env_vars:
        try:
            project_env = json.loads(project.env_vars)
        except (json.JSONDecodeError, TypeError):
            pass

    github_token: str | None = None
    if is_repo_url(repo.path):
        github_token = project_env.get("GITHUB_TOKEN") or settings.github_token

        if not github_token:
            raise HTTPException(
                422,
                "No GitHub token configured. Set WAVE_GITHUB_TOKEN in server config "
                "or GITHUB_TOKEN in project env vars to access remote repositories.",
            )

        # Verify the bot account can access and push to this repository
        accessible, detail = await _check_repo_accessible(
            repo.path,
            github_token,
        )
        if accessible is False:
            raise HTTPException(422, detail)

    # Agent CLI must be installed (pi or claude depending on runtime)
    runtime = settings.runtime
    if runtime == "pi":
        if not shutil.which("pi"):
            raise HTTPException(
                422,
                "The 'pi' CLI is not installed or not in PATH. "
                "Install it: npm install -g @mariozechner/pi-coding-agent",
            )
    elif not shutil.which("claude"):
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

    # PR target branch must exist on the remote (when configured)
    pr_target = project_env.get("GITHUB_PR_TARGET") or settings.github_pr_target
    if pr_target and is_repo_url(repo.path) and github_token:
        exists = await _check_remote_branch_exists(repo.path, pr_target, github_token)
        if exists is False:
            raise HTTPException(
                422,
                f"PR target branch '{pr_target}' does not exist on the remote "
                f"repository. Create it first or update the GITHUB_PR_TARGET setting.",
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
            "initiated_by": body.initiated_by,
            "reason": body.reason,
            "callback_url": body.callback_url,
        }
    )
    execution = Execution(
        sequence_id=sequence_id,
        runtime=body.runtime or settings.runtime,
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
async def list_executions(sequence_id: str, db: AsyncSession = Depends(get_db)):
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
async def cancel_execution(execution_id: str, db: AsyncSession = Depends(get_db)):
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
async def continue_execution(execution_id: str, db: AsyncSession = Depends(get_db)):
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
        continued_from=execution_id,
        trigger="continuation",
        runtime=exc.runtime,
        config=exc.config,
        source_branch=exc.source_branch,
        source_sha=exc.source_sha,
        work_branch=exc.work_branch,
    )
    db.add(new_exec)
    await db.commit()
    await db.refresh(new_exec)
    from wave_server.engine.execution_manager import launch_execution

    await launch_execution(new_exec.id, exc.sequence_id, continue_from=execution_id)
    return new_exec


@router.post(
    "/executions/{execution_id}/rerun",
    response_model=ExecutionResponse,
    status_code=201,
)
async def rerun_execution(
    execution_id: str, body: RerunRequest, db: AsyncSession = Depends(get_db)
):
    exc = await db.get(Execution, execution_id)
    if not exc:
        raise HTTPException(404, "Execution not found")
    if exc.status not in ("completed", "failed", "cancelled"):
        raise HTTPException(
            400, "Execution must be completed, failed, or cancelled to rerun tasks"
        )
    seq = await db.get(Sequence, exc.sequence_id)
    if not seq:
        raise HTTPException(404, "Sequence not found")
    await _preflight(exc.sequence_id, seq.project_id, db)

    # Validate that requested task IDs exist in the plan
    plan_content = storage.read_plan(exc.sequence_id)
    from wave_server.engine.plan_parser import parse_plan as _parse

    plan = _parse(plan_content)
    all_plan_ids: set[str] = set()
    for wave in plan.waves:
        for t in wave.foundation:
            all_plan_ids.add(t.id)
        for feature in wave.features:
            for t in feature.tasks:
                all_plan_ids.add(t.id)
        for t in wave.integration:
            all_plan_ids.add(t.id)
    unknown = set(body.task_ids) - all_plan_ids
    if unknown:
        raise HTTPException(422, f"Unknown task IDs: {', '.join(sorted(unknown))}")

    new_exec = Execution(
        sequence_id=exc.sequence_id,
        continued_from=execution_id,
        trigger="rerun",
        runtime=exc.runtime,
        config=exc.config,
        source_branch=exc.source_branch,
        source_sha=exc.source_sha,
        work_branch=exc.work_branch,
    )
    db.add(new_exec)
    await db.commit()
    await db.refresh(new_exec)
    from wave_server.engine.execution_manager import launch_execution

    await launch_execution(
        new_exec.id,
        exc.sequence_id,
        continue_from=execution_id,
        rerun_task_ids=set(body.task_ids),
        rerun_cascade=body.cascade,
    )
    return new_exec


# --- Events ---


@router.get("/executions/{execution_id}/events", response_model=list[EventResponse])
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
        .where(
            Event.event_type.in_(
                ["task_started", "task_completed", "task_failed", "task_skipped"]
            )
        )
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
async def list_task_logs(execution_id: str, db: AsyncSession = Depends(get_db)):
    exc = await db.get(Execution, execution_id)
    if not exc:
        raise HTTPException(404, "Execution not found")
    return storage.list_task_logs(execution_id)


@router.get("/executions/{execution_id}/task-logs/search")
async def search_task_logs(
    execution_id: str,
    q: str = Query(..., min_length=1, description="Search query"),
    agent: str = Query(
        "", description="Filter by agent: worker, test-writer, wave-verifier"
    ),
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
async def list_blockers(execution_id: str, db: AsyncSession = Depends(get_db)):
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


@router.post(
    "/executions/{execution_id}/promote",
    response_model=PromoteResponse,
)
async def promote_execution(
    execution_id: str,
    body: PromoteRequest | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Promote a completed execution: approve + merge its PR, then create a promotion PR.

    Uses the review-bot GitHub App to:
    1. Approve the execution's PR (e.g. into dev)
    2. Merge it (review-bot must be on the target branch's bypass list)
    3. Create a promotion PR to another branch (e.g. dev → main)

    The promotion PR requires human approval to merge.
    """
    if body is None:
        body = PromoteRequest()

    execution = await db.get(Execution, execution_id)
    if not execution:
        raise HTTPException(404, "Execution not found")

    if execution.status != "completed":
        raise HTTPException(
            400, f"Execution is '{execution.status}', must be 'completed' to promote"
        )

    if not execution.pr_url:
        raise HTTPException(400, "Execution has no PR to promote (was it pushed?)")

    # Resolve project env vars for per-project app overrides
    sequence = await db.get(Sequence, execution.sequence_id)
    if not sequence:
        raise HTTPException(404, "Sequence not found")

    from wave_server.models import Project

    project = await db.get(Project, sequence.project_id)
    project_env: dict[str, str] = {}
    if project and project.env_vars:
        import json

        try:
            project_env = json.loads(project.env_vars)
        except (json.JSONDecodeError, TypeError):
            pass

    # Resolve GitHub token for promote operations
    github_token = project_env.get("GITHUB_TOKEN") or settings.github_token
    if not github_token:
        raise HTTPException(
            400,
            "GitHub token not configured. "
            "Set WAVE_GITHUB_TOKEN in server config or GITHUB_TOKEN in project env vars.",
        )

    promotion_target = body.promotion_target or "main"

    result = await promote_pr(
        review_token=github_token,
        pr_url=execution.pr_url,
        promotion_target=promotion_target,
        merge_method=body.merge_method,
    )

    return PromoteResponse(
        success=result.success,
        merged_pr_url=result.merged_pr.url if result.merged_pr else None,
        promotion_pr_url=result.promotion_pr_url,
        error=result.error,
    )


@router.post("/promote", response_model=PromoteResponse)
async def standalone_promote(
    body: StandalonePromoteRequest,
    db: AsyncSession = Depends(get_db),
):
    """Promote a PR: approve + merge, then create a promotion PR.

    Standalone version — no execution ID needed. For quick-fixes
    and external PRs that aren't tied to a Wave execution.
    """
    github_token = settings.github_token
    if not github_token:
        raise HTTPException(
            400,
            "GitHub token not configured. Set WAVE_GITHUB_TOKEN in server config.",
        )

    promotion_target = body.promotion_target or "main"

    result = await promote_pr(
        review_token=github_token,
        pr_url=body.pr_url,
        promotion_target=promotion_target,
        merge_method=body.merge_method,
    )

    return PromoteResponse(
        success=result.success,
        merged_pr_url=result.merged_pr.url if result.merged_pr else None,
        promotion_pr_url=result.promotion_pr_url,
        error=result.error,
    )
