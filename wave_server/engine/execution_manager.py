"""Execution manager — launches and tracks background execution tasks.

Bridges the REST API with the wave executor engine. When an execution is
created, this module launches a background asyncio.Task that runs the
wave executor and pushes events to the database.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from wave_server.config import settings
from wave_server.db import async_session
from wave_server.engine.execution_logger import ExecutionLogger
from wave_server.engine.log_parser import format_task_log, parse_stream_json
from wave_server.engine.plan_parser import parse_plan
from wave_server.engine.dag import validate_plan
from wave_server.engine.runner import get_runner
from wave_server.engine.state import (
    create_initial_state,
    mark_task_done,
    mark_task_failed,
    state_to_json,
)
from wave_server.engine.types import ProgressUpdate, Task, TaskResult
from wave_server.engine.git_worktree import (
    branch_exists,
    build_signing_env,
    checkout_branch,
    create_pr,
    create_work_branch,
    get_current_branch,
    get_current_sha,
    get_remote_url,
    has_gh_cli,
    is_git_repo,
    push_branch,
    sha_exists,
)
from wave_server.config import settings as server_settings
from wave_server.engine.wave_executor import WaveExecutorOptions, execute_wave, _build_task_prompt
from wave_server.models import Event, Execution, ProjectContextFile, ProjectRepository, Sequence
from wave_server import storage

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
            cwd=cwd, capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


async def launch_execution(execution_id: str, sequence_id: str) -> None:
    """Launch a background execution task."""
    task = asyncio.create_task(_run_execution(execution_id, sequence_id))
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


async def _run_execution(execution_id: str, sequence_id: str) -> None:
    """Background task that runs the full wave execution."""
    # Lock to serialize all DB writes — prevents SQLite "database is locked"
    # when concurrent tasks try to commit at the same time.
    db_lock = asyncio.Lock()

    # Track git state for cleanup in error handlers
    use_git = False
    original_branch: str | None = None
    repo_cwd: str | None = None

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
                await db.commit()
                await _emit_event(
                    db, execution_id, "run_completed",
                    payload={"passed": False, "error": "No plan found"},
                )
                return

            # Parse plan
            plan = parse_plan(plan_content)
            valid, errors = validate_plan(plan)
            if not valid:
                execution.status = "failed"
                await db.commit()
                await _emit_event(
                    db, execution_id, "run_completed",
                    payload={"passed": False, "error": f"Plan validation failed: {'; '.join(errors)}"},
                )
                return

            # Update execution metadata
            total_tasks = sum(
                len(w.foundation) + sum(len(f.tasks) for f in w.features) + len(w.integration)
                for w in plan.waves
            )
            execution.status = "running"
            execution.total_tasks = total_tasks
            execution.started_at = datetime.now(timezone.utc)
            sequence.status = "executing"
            await db.commit()

            await _emit_event(db, execution_id, "run_started")

            # Get runner
            config = json.loads(execution.config or "{}")
            runner = get_runner(execution.runtime)
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
                server_agent_models["wave-verifier"] = settings.default_model_wave_verifier

            exec_agent_models: dict[str, str] | None = (
                {**server_agent_models, **(config.get("agent_models") or {})}
                or None
            )
            spec_content = storage.read_spec(sequence_id) or ""

            # Resolve repo path for execution cwd
            repo_result = await db.execute(
                select(ProjectRepository)
                .where(ProjectRepository.project_id == sequence.project_id)
                .limit(1)
            )
            repo = repo_result.scalar_one_or_none()
            repo_cwd = repo.path if repo and Path(repo.path).is_dir() else None

            if not repo_cwd:
                execution.status = "failed"
                await db.commit()
                await _emit_event(
                    db, execution_id, "run_completed",
                    payload={
                        "passed": False,
                        "error": "No repository configured for this project. "
                                 "Register one via POST /api/v1/projects/{project_id}/repositories",
                    },
                )
                return

            # ── Git branch setup ───────────────────────────────
            use_git = await is_git_repo(repo_cwd)

            if use_git:
                original_branch = await get_current_branch(repo_cwd)

                # Resolve source branch: explicit > current
                source_branch = execution.source_branch or original_branch or "main"
                execution.source_branch = source_branch

                # Resolve source SHA: explicit > HEAD of source branch
                if execution.source_sha:
                    if not await sha_exists(repo_cwd, execution.source_sha):
                        execution.status = "failed"
                        await db.commit()
                        await _emit_event(
                            db, execution_id, "run_completed",
                            payload={"passed": False, "error": f"Source SHA {execution.source_sha} not found in repo"},
                        )
                        return
                else:
                    execution.source_sha = await get_current_sha(repo_cwd)

                # Create work branch: wave/exec-{short_id}
                # With worktree isolation, feature worktrees branch from this.
                short_id = execution_id[:8]
                work_branch = f"wave/exec-{short_id}"
                execution.work_branch = work_branch

                start_point = execution.source_sha or source_branch

                if await branch_exists(repo_cwd, work_branch):
                    # Resume: reuse existing work branch (e.g. from /continue)
                    ok, err = await checkout_branch(repo_cwd, work_branch)
                else:
                    ok, err = await create_work_branch(repo_cwd, work_branch, start_point)

                if not ok:
                    # Try to restore original branch before failing
                    if original_branch:
                        await checkout_branch(repo_cwd, original_branch)
                    execution.status = "failed"
                    await db.commit()
                    await _emit_event(
                        db, execution_id, "run_completed",
                        payload={"passed": False, "error": f"Cannot create work branch: {err}"},
                    )
                    return

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

            # Inject server-wide GitHub token (project env can override)
            github_token = project_env.get("GITHUB_TOKEN") or server_settings.github_token
            if github_token and "GITHUB_TOKEN" not in project_env:
                project_env["GITHUB_TOKEN"] = github_token
                project_env["GH_TOKEN"] = github_token

            # Inject git identity for agent commits
            if server_settings.git_committer_name and "GIT_COMMITTER_NAME" not in project_env:
                project_env["GIT_COMMITTER_NAME"] = server_settings.git_committer_name
                project_env["GIT_AUTHOR_NAME"] = server_settings.git_committer_name
            if server_settings.git_committer_email and "GIT_COMMITTER_EMAIL" not in project_env:
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
                db, execution_id, "branch_created",
                payload={
                    "source_branch": execution.source_branch,
                    "source_sha": execution.source_sha,
                    "work_branch": execution.work_branch,
                },
            )

            # Load state for resume
            state = create_initial_state("plan.md")
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

            def _flush_log():
                """Write execution log to disk (called frequently for live tailing)."""
                storage.write_log(execution_id, exec_logger.render())

            _flush_log()

            # Execute waves
            all_passed = True
            completed_count = 0

            for wave_idx, wave in enumerate(plan.waves):
                async with db_lock:
                    execution.current_wave = wave_idx
                    await db.commit()

                    await _emit_event(
                        db, execution_id, "phase_changed",
                        payload={"wave_index": wave_idx, "wave_name": wave.name},
                    )

                exec_logger.wave_started(wave.name, wave_idx)
                _flush_log()

                async def on_task_start(phase: str, task: Task):
                    exec_logger.task_started(phase, task)
                    _flush_log()
                    async with db_lock:
                        await _emit_event(
                            db, execution_id, "task_started",
                            task_id=task.id, phase=phase,
                            payload={"task_id": task.id, "title": task.title, "agent": task.agent, "phase": phase},
                        )

                async def on_task_end(phase: str, task: Task, result: TaskResult):
                    nonlocal completed_count
                    completed_count += 1

                    # Log to execution logger
                    exec_logger.task_ended(phase, task, result)

                    event_type = "task_completed" if result.exit_code == 0 else (
                        "task_skipped" if result.exit_code == -1 else "task_failed"
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
                        storage.write_transcript(execution_id, task.id, header + result.stdout)
                    # Write structured task log (human-readable) + collect cost
                    try:
                        prompt = _build_task_prompt(task, spec_content, plan.data_schemas)
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
                        storage.write_task_log(execution_id, task.id, task_log, task.agent)
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
                            db, execution_id, event_type,
                            task_id=task.id, phase=phase,
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
                    project_context=project_context,
                    cwd=repo_cwd,
                    env=project_env or None,
                    max_concurrency=max_concurrency,
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
                    execution.waves_state = json.dumps({
                        "waves": [
                            {"name": wave.name, "index": wave_idx, "passed": wave_result.passed}
                        ]
                    })
                    await db.commit()

                    await _emit_event(
                        db, execution_id, "wave_completed",
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

            if all_passed and use_git and execution.work_branch and execution.source_branch:
                # Try to push the work branch
                remote_url = await get_remote_url(repo_cwd)
                if remote_url:
                    push_ok, push_err = await push_branch(
                        repo_cwd, execution.work_branch,
                        github_token=github_token,
                    )
                    if push_ok:
                        # Try to create a PR via gh CLI
                        if await has_gh_cli():
                            seq_name = sequence.name if sequence else "execution"
                            pr_title = f"wave: {seq_name}"
                            pr_body = (
                                f"Automated PR from wave execution `{execution_id[:8]}`.\n\n"
                                f"**Goal:** {plan.goal or 'N/A'}\n"
                                f"**Tasks:** {completed_count}/{total_tasks} completed\n"
                                f"**Waves:** {len(plan.waves)}\n"
                                f"**Source:** `{execution.source_branch}` @ `{execution.source_sha[:8] if execution.source_sha else 'N/A'}`"
                            )
                            pr_url, pr_err = await create_pr(
                                repo_cwd,
                                execution.work_branch,
                                execution.source_branch,
                                pr_title,
                                pr_body,
                                github_token=github_token,
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
                    pr_error = "No remote configured — work branch available locally"

            # Restore original branch (best-effort)
            if use_git and original_branch and original_branch != execution.work_branch:
                await checkout_branch(repo_cwd, original_branch)

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
                db, execution_id, "run_completed",
                payload=run_payload,
            )

        except asyncio.CancelledError:
            # Restore original branch on cancellation (best-effort)
            if use_git and original_branch and repo_cwd:
                await checkout_branch(repo_cwd, original_branch)
            async with async_session() as db2:
                execution = await db2.get(Execution, execution_id)
                if execution:
                    execution.status = "cancelled"
                    execution.finished_at = datetime.now(timezone.utc)
                    seq = await db2.get(Sequence, sequence_id)
                    if seq:
                        seq.status = "cancelled"
                    await db2.commit()
            raise

        except Exception as e:
            # Restore original branch on failure (best-effort)
            if use_git and original_branch and repo_cwd:
                await checkout_branch(repo_cwd, original_branch)
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
                    db2, execution_id, "run_completed",
                    payload={"passed": False, "error": str(e)},
                )



