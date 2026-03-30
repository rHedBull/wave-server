"""Execution manager — launches and tracks background execution tasks.

Bridges the REST API with the wave executor engine. When an execution is
created, this module launches a background asyncio.Task that runs the
wave executor and pushes events to the database.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from wave_server import storage
from wave_server.config import settings
from wave_server.config import settings as server_settings
from wave_server.db import async_session
from wave_server.engine.dag import validate_plan
from wave_server.engine.execution_logger import ExecutionLogger
from wave_server.engine.git_worktree import (
    build_signing_env,
    create_execution_worktree,
    create_pr,
    get_current_branch,
    get_current_sha,
    get_remote_url,
    has_gh_cli,
    is_git_repo,
    push_branch,
    remove_execution_worktree,
    sha_exists,
)
from wave_server.engine.log_parser import (
    format_task_log,
    parse_pi_json,
    parse_stream_json,
)
from wave_server.engine.plan_parser import parse_plan
from wave_server.engine.rate_limit import RateLimitAwareRunner, RateLimitPauser
from wave_server.engine.repo_cache import is_repo_url, ensure_repo
from wave_server.engine.runner import get_runner
from wave_server.engine.state import (
    create_initial_state,
    mark_task_done,
    mark_task_failed,
)
from wave_server.engine.types import Task, TaskResult
from wave_server.engine.wave_executor import (
    WaveExecutorOptions,
    execute_wave,
    _build_task_prompt,
)
from wave_server.models import (
    Event,
    Execution,
    ProjectContextFile,
    ProjectRepository,
    Sequence,
)

_callback_log = logging.getLogger("wave_server.callback")

# Track active execution tasks
_active_tasks: dict[str, asyncio.Task] = {}

# Max size per context file to avoid prompt bloat (32KB)
_MAX_CONTEXT_FILE_SIZE = 32 * 1024


def _load_context_files(context_files: list, repo_cwd: str) -> str:
    """Load project context files and return combined content for prompt injection."""
    if not context_files:
        return ""

    sections: list[str] = []
    for cf in context_files:
        file_path = Path(cf.path)
        if not file_path.is_absolute():
            file_path = Path(repo_cwd) / file_path
        file_path = file_path.resolve()

        if not file_path.is_file():
            continue

        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
            if len(content) > _MAX_CONTEXT_FILE_SIZE:
                content = content[:_MAX_CONTEXT_FILE_SIZE] + "\n... (truncated)"
            label = cf.description or cf.path
            sections.append(f"### {label}\n```\n{content}\n```")
        except OSError:
            continue

    if not sections:
        return ""

    return "## Project Context\n\n" + "\n\n".join(sections)


def _get_git_sha(cwd: str) -> str | None:
    """Get current HEAD SHA for a git repo. Returns None if not a git repo."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


async def launch_execution(
    execution_id: str,
    sequence_id: str,
    continue_from: str | None = None,
    rerun_task_ids: set[str] | None = None,
    rerun_cascade: bool = True,
) -> None:
    """Launch a background execution task.

    If *continue_from* is set, the new execution resumes where the previous
    one left off — already-completed tasks are skipped.

    If *rerun_task_ids* is also set, only the specified tasks (and optionally
    their downstream dependents when *rerun_cascade* is True) are re-executed.
    """
    task = asyncio.create_task(
        _run_execution(
            execution_id, sequence_id, continue_from, rerun_task_ids, rerun_cascade
        )
    )
    _active_tasks[execution_id] = task
    task.add_done_callback(lambda _: _active_tasks.pop(execution_id, None))


def get_active_count() -> int:
    return len(_active_tasks)


def cancel_execution(execution_id: str) -> bool:
    task = _active_tasks.get(execution_id)
    if task:
        task.cancel()
        return True
    return False


async def _fire_callback(
    config: dict,
    execution_id: str,
    status: str,
    total_tasks: int = 0,
    completed_tasks: int = 0,
    duration_ms: int = 0,
    pr_url: str | None = None,
    work_branch: str | None = None,
    error: str | None = None,
) -> None:
    """POST completion payload to callback_url if configured. Best-effort, never raises."""
    callback_url = config.get("callback_url")
    if not callback_url:
        return
    payload = {
        "execution_id": execution_id,
        "status": status,
        "total_tasks": total_tasks,
        "completed_tasks": completed_tasks,
        "duration_ms": duration_ms,
        "pr_url": pr_url,
        "work_branch": work_branch,
        "error": error,
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(callback_url, json=payload)
            _callback_log.info(
                "Callback to %s: status=%d payload=%s",
                callback_url,
                resp.status_code,
                json.dumps(payload),
            )
    except Exception as e:
        _callback_log.warning("Callback to %s failed: %s", callback_url, e)


async def _emit_event(
    db: AsyncSession,
    execution_id: str,
    event_type: str,
    task_id: str | None = None,
    phase: str | None = None,
    payload: dict | None = None,
) -> None:
    event = Event(
        execution_id=execution_id,
        event_type=event_type,
        task_id=task_id,
        phase=phase,
        payload=json.dumps(payload or {}),
    )
    db.add(event)
    await db.commit()


async def _get_completed_task_ids(db: AsyncSession, execution_id: str) -> set[str]:
    """Return the set of task IDs that completed successfully in a previous execution."""
    result = await db.execute(
        select(Event.task_id)
        .where(Event.execution_id == execution_id)
        .where(Event.event_type == "task_completed")
        .where(Event.task_id.isnot(None))
    )
    return {row[0] for row in result.all()}


async def _run_execution(
    execution_id: str,
    sequence_id: str,
    continue_from: str | None = None,
    rerun_task_ids: set[str] | None = None,
    rerun_cascade: bool = True,
) -> None:
    """Background task that runs the full wave execution."""
    # Lock to serialize all DB writes — prevents SQLite "database is locked"
    # when concurrent tasks try to commit at the same time.
    db_lock = asyncio.Lock()

    # Track git state for cleanup in error handlers
    use_git = False
    original_branch: str | None = None
    repo_cwd: str | None = None
    repo_root: str | None = None  # original repo path (never changes)
    exec_worktree_dir: str | None = None  # execution-level worktree (cleanup on error)

    async with async_session() as db:
        try:
            # Load execution and sequence
            execution = await db.get(Execution, execution_id)
            sequence = await db.get(Sequence, sequence_id)
            if not execution or not sequence:
                return

            # Load plan content
            plan_content = storage.read_plan(sequence_id)
            if not plan_content:
                execution.status = "failed"
                execution.finished_at = datetime.now(timezone.utc)
                await db.commit()
                await _emit_event(
                    db,
                    execution_id,
                    "run_completed",
                    payload={"passed": False, "error": "No plan found"},
                )
                return

            # Parse plan
            plan = parse_plan(plan_content)
            valid, errors = validate_plan(plan)
            if not valid:
                execution.status = "failed"
                execution.finished_at = datetime.now(timezone.utc)
                await db.commit()
                await _emit_event(
                    db,
                    execution_id,
                    "run_completed",
                    payload={
                        "passed": False,
                        "error": f"Plan validation failed: {'; '.join(errors)}",
                    },
                )
                return

            # Update execution metadata
            total_tasks = sum(
                len(w.foundation)
                + sum(len(f.tasks) for f in w.features)
                + len(w.integration)
                for w in plan.waves
            )
            execution.status = "running"
            execution.total_tasks = total_tasks
            execution.started_at = datetime.now(timezone.utc)
            sequence.status = "running"
            await db.commit()

            await _emit_event(db, execution_id, "run_started")

            # Get runner
            config = json.loads(execution.config or "{}")
            base_runner = get_runner(execution.runtime)
            max_concurrency = config.get("concurrency") or settings.default_concurrency
            # Model resolution: per-execution > server default
            exec_model: str | None = config.get("model") or settings.default_model

            # Build agent_models: server-level per-agent defaults, overridden by execution-level
            server_agent_models: dict[str, str] = {}
            if settings.default_model_worker:
                server_agent_models["worker"] = settings.default_model_worker
            if settings.default_model_test_writer:
                server_agent_models["test-writer"] = settings.default_model_test_writer
            if settings.default_model_wave_verifier:
                server_agent_models["wave-verifier"] = (
                    settings.default_model_wave_verifier
                )

            exec_agent_models: dict[str, str] | None = {
                **server_agent_models,
                **(config.get("agent_models") or {}),
            } or None
            spec_content = storage.read_spec(sequence_id) or ""

            # ── Rate-limit pause-and-resume ────────────────────
            pauser: RateLimitPauser | None = None
            runner = base_runner
            # Mutable ref so pauser callbacks (created early) can log
            # through the exec_logger (created later).
            _log_ref: dict = {"logger": None, "flush": None}

            if settings.rate_limit_enabled:

                async def _on_rate_limit_pause(wait_seconds: int, resume_at: datetime):
                    wait_min = wait_seconds // 60
                    resume_str = resume_at.strftime("%H:%M UTC")
                    if _log_ref["logger"]:
                        _log_ref["logger"].log(
                            f"⏸️  **Rate limit hit** — pausing for {wait_min} min "
                            f"(resume ≈ {resume_str})"
                        )
                        if _log_ref["flush"]:
                            _log_ref["flush"]()
                    async with db_lock:
                        execution.status = "paused"
                        execution.paused_until = resume_at
                        await db.commit()
                        await _emit_event(
                            db,
                            execution_id,
                            "execution_paused",
                            payload={
                                "wait_seconds": wait_seconds,
                                "resume_at": resume_at.isoformat(),
                            },
                        )

                async def _on_rate_limit_resume():
                    if _log_ref["logger"]:
                        _log_ref["logger"].log(
                            "▶️  **Resuming** — rate limit window reset"
                        )
                        if _log_ref["flush"]:
                            _log_ref["flush"]()
                    async with db_lock:
                        execution.status = "running"
                        execution.paused_until = None
                        await db.commit()
                        await _emit_event(
                            db,
                            execution_id,
                            "execution_resumed",
                        )

                pauser = RateLimitPauser(
                    wait_seconds=settings.rate_limit_pause_seconds,
                    on_pause=_on_rate_limit_pause,
                    on_resume=_on_rate_limit_resume,
                )
                runner = RateLimitAwareRunner(
                    inner=base_runner,
                    pauser=pauser,
                    max_retries=settings.rate_limit_max_retries,
                )

            # Resolve repo path for execution cwd
            repo_result = await db.execute(
                select(ProjectRepository)
                .where(ProjectRepository.project_id == sequence.project_id)
                .limit(1)
            )
            repo = repo_result.scalar_one_or_none()

            if not repo:
                execution.status = "failed"
                execution.finished_at = datetime.now(timezone.utc)
                await db.commit()
                await _emit_event(
                    db,
                    execution_id,
                    "run_completed",
                    payload={
                        "passed": False,
                        "error": "No repository configured for this project. "
                        "Register one via POST /api/v1/projects/{project_id}/repositories",
                    },
                )
                return

            # Resolve repo path: remote URL → cached local clone, local path → use directly
            if is_repo_url(repo.path):
                # Remote repo — ensure local clone exists and is up to date
                clone_token = server_settings.github_token

                repos_dir = str((settings.data_dir / "repos").resolve())
                repo_cwd, clone_err = await ensure_repo(
                    repo.path, repos_dir, clone_token
                )
                if not repo_cwd:
                    execution.status = "failed"
                    execution.finished_at = datetime.now(timezone.utc)
                    await db.commit()
                    await _emit_event(
                        db,
                        execution_id,
                        "run_completed",
                        payload={
                            "passed": False,
                            "error": f"Failed to sync repo: {clone_err}",
                        },
                    )
                    return
            else:
                repo_cwd = repo.path if Path(repo.path).is_dir() else None

            if not repo_cwd:
                execution.status = "failed"
                execution.finished_at = datetime.now(timezone.utc)
                await db.commit()
                await _emit_event(
                    db,
                    execution_id,
                    "run_completed",
                    payload={
                        "passed": False,
                        "error": f"Repository path not found: {repo.path}",
                    },
                )
                return

            # ── Git branch setup ───────────────────────────────
            use_git = await is_git_repo(repo_cwd)
            repo_root = repo_cwd  # preserve original repo path

            if use_git:
                original_branch = await get_current_branch(repo_cwd)

                # Resolve source branch: explicit > current
                source_branch = execution.source_branch or original_branch or "main"
                execution.source_branch = source_branch

                # Resolve source SHA: explicit > HEAD of source branch
                if execution.source_sha:
                    if not await sha_exists(repo_cwd, execution.source_sha):
                        execution.status = "failed"
                        execution.finished_at = datetime.now(timezone.utc)
                        await db.commit()
                        await _emit_event(
                            db,
                            execution_id,
                            "run_completed",
                            payload={
                                "passed": False,
                                "error": f"Source SHA {execution.source_sha} not found in repo",
                            },
                        )
                        return
                else:
                    execution.source_sha = await get_current_sha(repo_cwd)

                # Create work branch: wave/exec-{short_id}
                # With worktree isolation, feature worktrees branch from this.
                # For continuations, reuse the previous execution's work branch.
                if execution.work_branch:
                    work_branch = execution.work_branch
                else:
                    short_id = execution_id[:8]
                    work_branch = f"wave/exec-{short_id}"
                    execution.work_branch = work_branch

                start_point = execution.source_sha or source_branch

                # Use a worktree so the user's working tree is never disturbed.
                wt_dir, err = await create_execution_worktree(
                    repo_cwd, work_branch, start_point
                )
                if not wt_dir:
                    execution.status = "failed"
                    execution.finished_at = datetime.now(timezone.utc)
                    await db.commit()
                    await _emit_event(
                        db,
                        execution_id,
                        "run_completed",
                        payload={
                            "passed": False,
                            "error": f"Cannot create work branch: {err}",
                        },
                    )
                    return

                exec_worktree_dir = wt_dir
                # From here on, all work happens inside the worktree —
                # repo_cwd is redirected so downstream code (wave executor,
                # feature worktrees, push, etc.) operates in the right place.
                repo_cwd = exec_worktree_dir

                await db.commit()

            # Load project context files
            ctx_result = await db.execute(
                select(ProjectContextFile)
                .where(ProjectContextFile.project_id == sequence.project_id)
                .order_by(ProjectContextFile.created_at)
            )
            context_files = ctx_result.scalars().all()
            project_context = _load_context_files(context_files, repo_cwd)

            # Load project environment variables
            from wave_server.models import Project

            project = await db.get(Project, sequence.project_id)
            project_env: dict[str, str] = {}
            if project and project.env_vars:
                try:
                    project_env = json.loads(project.env_vars)
                except (json.JSONDecodeError, TypeError):
                    pass

            # Inject model — project env can override, otherwise use server default
            if "ANTHROPIC_MODEL" not in project_env:
                project_env["ANTHROPIC_MODEL"] = server_settings.default_model

            # Inject server-wide GitHub token (project env can override)
            github_token = (
                project_env.get("GITHUB_TOKEN") or server_settings.github_token
            )
            if github_token and "GITHUB_TOKEN" not in project_env:
                project_env["GITHUB_TOKEN"] = github_token
                project_env["GH_TOKEN"] = github_token

            # Resolve PR target branch (project env > server config > source branch)
            pr_target_branch = (
                project_env.get("GITHUB_PR_TARGET") or server_settings.github_pr_target
                # fallback to source_branch happens at PR creation time
            )

            # Inject git identity for agent commits
            if (
                server_settings.git_committer_name
                and "GIT_COMMITTER_NAME" not in project_env
            ):
                project_env["GIT_COMMITTER_NAME"] = server_settings.git_committer_name
                project_env["GIT_AUTHOR_NAME"] = server_settings.git_committer_name
            if (
                server_settings.git_committer_email
                and "GIT_COMMITTER_EMAIL" not in project_env
            ):
                project_env["GIT_COMMITTER_EMAIL"] = server_settings.git_committer_email
                project_env["GIT_AUTHOR_EMAIL"] = server_settings.git_committer_email

            # Inject commit signing config (env-only, never touches .git/config)
            if server_settings.git_signing_key:
                signing_env = build_signing_env(server_settings.git_signing_key)
                for k, v in signing_env.items():
                    if k not in project_env:
                        project_env[k] = v

            # Capture git SHA before execution (now on the work branch)
            execution.git_sha_before = _get_git_sha(repo_cwd)
            await db.commit()

            await _emit_event(
                db,
                execution_id,
                "branch_created",
                payload={
                    "source_branch": execution.source_branch,
                    "source_sha": execution.source_sha,
                    "work_branch": execution.work_branch,
                },
            )

            # Load state for resume
            state = create_initial_state("plan.md")

            # Build skip set from previous execution's completed tasks
            skip_task_ids: set[str] = set()
            if continue_from:
                skip_task_ids = await _get_completed_task_ids(db, continue_from)

                # For rerun: remove dirty tasks from the skip set
                if rerun_task_ids:
                    from wave_server.engine.dag import compute_dirty_closure

                    dirty = compute_dirty_closure(
                        plan, rerun_task_ids, cascade=rerun_cascade
                    )
                    skip_task_ids -= dirty

                # Pre-mark completed tasks in state so DAG dependencies are satisfied
                for tid in skip_task_ids:
                    mark_task_done(state, tid)

            start_time = time.monotonic()

            # ── Execution Logger ───────────────────────────────
            exec_logger = ExecutionLogger(
                execution_id=execution_id,
                runtime=execution.runtime,
                total_tasks=total_tasks,
                max_concurrency=max_concurrency,
                goal=plan.goal or "",
                wave_count=len(plan.waves),
            )
            exec_logger.execution_started()

            if skip_task_ids and continue_from:
                if rerun_task_ids:
                    exec_logger.log(
                        f"🔄  Rerunning from execution {continue_from[:8]}… "
                        f"— {len(skip_task_ids)} completed tasks will be skipped"
                        + (" (cascade)" if rerun_cascade else " (isolated)")
                    )
                else:
                    exec_logger.log(
                        f"♻️  Continuing from execution {continue_from[:8]}… "
                        f"— {len(skip_task_ids)} completed tasks will be skipped"
                    )

            def _flush_log():
                """Write execution log to disk (called frequently for live tailing)."""
                storage.write_log(execution_id, exec_logger.render())

            # Wire up logger ref so rate-limit callbacks can log
            _log_ref["logger"] = exec_logger
            _log_ref["flush"] = _flush_log

            _flush_log()

            # Execute waves
            all_passed = True
            completed_count = 0

            for wave_idx, wave in enumerate(plan.waves):
                async with db_lock:
                    execution.current_wave = wave_idx
                    await db.commit()

                    await _emit_event(
                        db,
                        execution_id,
                        "phase_changed",
                        payload={"wave_index": wave_idx, "wave_name": wave.name},
                    )

                exec_logger.wave_started(wave.name, wave_idx)
                _flush_log()

                async def on_task_start(phase: str, task: Task):
                    exec_logger.task_started(phase, task)
                    _flush_log()
                    async with db_lock:
                        await _emit_event(
                            db,
                            execution_id,
                            "task_started",
                            task_id=task.id,
                            phase=phase,
                            payload={
                                "task_id": task.id,
                                "title": task.title,
                                "agent": task.agent,
                                "phase": phase,
                            },
                        )

                async def on_task_end(phase: str, task: Task, result: TaskResult):
                    nonlocal completed_count
                    completed_count += 1

                    # Log to execution logger
                    exec_logger.task_ended(phase, task, result)

                    event_type = (
                        "task_completed"
                        if result.exit_code == 0
                        else (
                            "task_skipped" if result.exit_code == -1 else "task_failed"
                        )
                    )
                    # Save task output (extracted final result)
                    if result.output:
                        storage.write_output(execution_id, task.id, result.output)
                    # Save raw transcript
                    if result.stdout:
                        header = (
                            f"# Task: {task.id} — {result.title}\n"
                            f"Agent: {result.agent}\n"
                            f"Phase: {phase}\n"
                            f"Started: {datetime.now(timezone.utc).isoformat()}\n"
                            f"Duration: {result.duration_ms}ms\n"
                            f"Exit code: {result.exit_code}\n"
                            f"---\n"
                        )
                        storage.write_transcript(
                            execution_id, task.id, header + result.stdout
                        )
                    # Write structured task log (human-readable) + collect cost
                    try:
                        prompt = _build_task_prompt(
                            task,
                            spec_content,
                            plan.data_schemas,
                            plan.project_structure,
                            plan.environment,
                        )
                        if execution.runtime == "pi":
                            parsed = parse_pi_json(result.stdout or "")
                        else:
                            parsed = parse_stream_json(result.stdout or "")
                        task_log = format_task_log(
                            task_id=task.id,
                            title=task.title,
                            agent=task.agent,
                            phase=phase,
                            exit_code=result.exit_code,
                            duration_ms=result.duration_ms,
                            timed_out=result.timed_out,
                            prompt=prompt,
                            parsed=parsed,
                            extracted_output=result.output,
                        )
                        storage.write_task_log(
                            execution_id, task.id, task_log, task.agent
                        )
                        # Accumulate cost/tokens into execution logger
                        exec_logger.add_cost(
                            parsed.total_cost_usd,
                            parsed.input_tokens,
                            parsed.output_tokens,
                        )
                    except Exception:
                        pass  # Best effort — don't break execution for log formatting
                    _flush_log()
                    # Serialize all DB writes under lock to prevent SQLite contention
                    async with db_lock:
                        await _emit_event(
                            db,
                            execution_id,
                            event_type,
                            task_id=task.id,
                            phase=phase,
                            payload={
                                "task_id": task.id,
                                "exit_code": result.exit_code,
                                "duration_ms": result.duration_ms,
                            },
                        )
                        # Update execution count
                        execution.completed_tasks = completed_count
                        await db.commit()
                    # Update state
                    if result.exit_code == 0:
                        mark_task_done(state, task.id)
                    else:
                        mark_task_failed(state, task.id)

                def on_log(line: str):
                    exec_logger.log(line)
                    _flush_log()

                opts = WaveExecutorOptions(
                    wave=wave,
                    wave_num=wave_idx + 1,
                    runner=runner,
                    spec_content=spec_content,
                    data_schemas=plan.data_schemas,
                    project_structure=plan.project_structure,
                    environment=plan.environment,
                    project_context=project_context,
                    cwd=repo_cwd,
                    env=project_env or None,
                    max_concurrency=max_concurrency,
                    skip_task_ids=skip_task_ids,
                    repo_root=repo_cwd if use_git else None,
                    use_worktrees=use_git,
                    model=exec_model,
                    agent_models=exec_agent_models,
                    on_task_start=on_task_start,
                    on_task_end=on_task_end,
                    on_log=on_log,
                )

                wave_result = await execute_wave(opts)

                exec_logger.wave_ended(wave.name, wave_idx, passed=wave_result.passed)
                _flush_log()

                async with db_lock:
                    # Save waves state
                    execution.waves_state = json.dumps(
                        {
                            "waves": [
                                {
                                    "name": wave.name,
                                    "index": wave_idx,
                                    "passed": wave_result.passed,
                                }
                            ]
                        }
                    )
                    await db.commit()

                    await _emit_event(
                        db,
                        execution_id,
                        "wave_completed",
                        payload={"wave_name": wave.name, "passed": wave_result.passed},
                    )

                if not wave_result.passed:
                    all_passed = False
                    break

            # Capture git SHA after execution
            if repo_cwd:
                execution.git_sha_after = _get_git_sha(repo_cwd)

            # ── Push + PR (best-effort, only on success) ───────
            pr_url: str | None = None
            pr_error: str | None = None

            if (
                all_passed
                and use_git
                and execution.work_branch
                and execution.source_branch
            ):
                push_token = github_token

                if push_token:
                    # Try to push the work branch
                    remote_url = await get_remote_url(repo_cwd)
                    if remote_url:
                        push_ok, push_err = await push_branch(
                            repo_cwd,
                            execution.work_branch,
                            github_token=push_token,
                        )
                        if push_ok:
                            # Try to create a PR via gh CLI
                            if await has_gh_cli():
                                # PR targets configured branch (e.g. "dev") or falls back to source
                                target = pr_target_branch or execution.source_branch
                                seq_name = sequence.name if sequence else "execution"
                                pr_title = f"wave: {seq_name}"

                                # Extract provenance from execution config
                                _config = {}
                                if execution.config:
                                    try:
                                        _config = json.loads(execution.config)
                                    except Exception:
                                        pass
                                initiated_by = _config.get("initiated_by")
                                reason = _config.get("reason")

                                pr_body = f"Automated PR from wave execution `{execution_id[:8]}`.\n\n"
                                if initiated_by or reason:
                                    pr_body += "## Provenance\n"
                                    if initiated_by:
                                        pr_body += f"**Initiated by:** {initiated_by}\n"
                                    if reason:
                                        pr_body += f"**Reason:** {reason}\n"
                                    pr_body += "\n"
                                pr_body += (
                                    f"**Goal:** {plan.goal or 'N/A'}\n"
                                    f"**Tasks:** {completed_count}/{total_tasks} completed\n"
                                    f"**Waves:** {len(plan.waves)}\n"
                                    f"**Source:** `{execution.source_branch}` @ `{execution.source_sha[:8] if execution.source_sha else 'N/A'}`\n"
                                    f"**Target:** `{target}`"
                                )
                                pr_url, pr_err = await create_pr(
                                    repo_cwd,
                                    execution.work_branch,
                                    target,
                                    pr_title,
                                    pr_body,
                                    github_token=push_token,
                                )
                                if pr_url:
                                    execution.pr_url = pr_url
                                else:
                                    pr_error = pr_err
                            else:
                                pr_error = "gh CLI not available — branch pushed but PR not created"
                        else:
                            pr_error = push_err
                    else:
                        pr_error = (
                            "No remote configured — work branch available locally"
                        )

            # Clean up execution worktree (branch is preserved for push/PR)
            if exec_worktree_dir and repo_root:
                await remove_execution_worktree(repo_root, exec_worktree_dir)

            # Complete
            duration_ms = int((time.monotonic() - start_time) * 1000)
            execution.status = "completed" if all_passed else "failed"
            execution.completed_tasks = completed_count
            execution.finished_at = datetime.now(timezone.utc)
            sequence.status = "completed" if all_passed else "failed"
            await db.commit()

            # Finalize execution log
            exec_logger.execution_finished(all_passed=all_passed)
            _flush_log()

            run_payload: dict = {
                "passed": all_passed,
                "total_tasks": total_tasks,
                "completed_tasks": completed_count,
                "duration_ms": duration_ms,
                "work_branch": execution.work_branch,
            }
            if pr_url:
                run_payload["pr_url"] = pr_url
            if pr_error:
                run_payload["pr_note"] = pr_error

            await _emit_event(
                db,
                execution_id,
                "run_completed",
                payload=run_payload,
            )

            # Fire callback to external system (e.g. backbone)
            await _fire_callback(
                config,
                execution_id,
                status="completed" if all_passed else "failed",
                total_tasks=total_tasks,
                completed_tasks=completed_count,
                duration_ms=duration_ms,
                pr_url=pr_url,
                work_branch=execution.work_branch,
                error=None if all_passed else "One or more tasks failed",
            )

        except asyncio.CancelledError:
            # Cancel any pending rate-limit pause
            if pauser:
                pauser.cancel()
            # Clean up execution worktree on cancellation (best-effort)
            if exec_worktree_dir and repo_root:
                await remove_execution_worktree(repo_root, exec_worktree_dir)
            async with async_session() as db2:
                execution = await db2.get(Execution, execution_id)
                if execution:
                    execution.status = "cancelled"
                    execution.finished_at = datetime.now(timezone.utc)
                    seq = await db2.get(Sequence, sequence_id)
                    if seq:
                        seq.status = "cancelled"
                    await db2.commit()
            # Fire callback for cancellation
            await _fire_callback(
                config,
                execution_id,
                status="cancelled",
            )
            raise

        except Exception as e:
            # Cancel any pending rate-limit pause
            if pauser:
                pauser.cancel()
            # Clean up execution worktree on failure (best-effort)
            if exec_worktree_dir and repo_root:
                await remove_execution_worktree(repo_root, exec_worktree_dir)
            async with async_session() as db2:
                execution = await db2.get(Execution, execution_id)
                if execution:
                    execution.status = "failed"
                    execution.finished_at = datetime.now(timezone.utc)
                    seq = await db2.get(Sequence, sequence_id)
                    if seq:
                        seq.status = "failed"
                    await db2.commit()
                await _emit_event(
                    db2,
                    execution_id,
                    "run_completed",
                    payload={"passed": False, "error": str(e)},
                )
            # Fire callback for failure
            await _fire_callback(
                config,
                execution_id,
                status="failed",
                error=str(e),
            )
