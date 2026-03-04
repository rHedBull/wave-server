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
from wave_server.engine.wave_executor import WaveExecutorOptions, execute_wave, _build_task_prompt
from wave_server.models import Event, Execution, ProjectRepository, Sequence
from wave_server import storage

# Track active execution tasks
_active_tasks: dict[str, asyncio.Task] = {}


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
            await db.commit()

            await _emit_event(db, execution_id, "run_started")

            # Get runner
            config = json.loads(execution.config or "{}")
            runner = get_runner(execution.runtime)
            max_concurrency = config.get("concurrency") or settings.default_concurrency
            spec_content = storage.read_spec(sequence_id) or ""

            # Resolve repo path for git SHA capture
            repo_result = await db.execute(
                select(ProjectRepository)
                .where(ProjectRepository.project_id == sequence.project_id)
                .limit(1)
            )
            repo = repo_result.scalar_one_or_none()
            repo_cwd = repo.path if repo and Path(repo.path).is_dir() else None

            # Capture git SHA before execution
            if repo_cwd:
                execution.git_sha_before = _get_git_sha(repo_cwd)
                await db.commit()

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
                execution.current_wave = wave_idx
                await db.commit()

                await _emit_event(
                    db, execution_id, "phase_changed",
                    payload={"wave_index": wave_idx, "wave_name": wave.name},
                )

                exec_logger.wave_started(wave.name, wave_idx)
                _flush_log()

                def on_task_start(phase: str, task: Task):
                    exec_logger.task_started(phase, task)
                    _flush_log()
                    # Schedule coroutine to emit event
                    asyncio.create_task(_emit_event(
                        db, execution_id, "task_started",
                        task_id=task.id, phase=phase,
                        payload={"task_id": task.id, "title": task.title, "agent": task.agent, "phase": phase},
                    ))

                def on_task_end(phase: str, task: Task, result: TaskResult):
                    nonlocal completed_count
                    completed_count += 1

                    # Log to execution logger
                    exec_logger.task_ended(phase, task, result)

                    event_type = "task_completed" if result.exit_code == 0 else (
                        "task_skipped" if result.exit_code == -1 else "task_failed"
                    )
                    asyncio.create_task(_emit_event(
                        db, execution_id, event_type,
                        task_id=task.id, phase=phase,
                        payload={
                            "task_id": task.id,
                            "exit_code": result.exit_code,
                            "duration_ms": result.duration_ms,
                        },
                    ))
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
                    # Update execution count
                    asyncio.create_task(_update_completed_count(execution_id, completed_count))
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
                    cwd=".",
                    max_concurrency=max_concurrency,
                    on_task_start=on_task_start,
                    on_task_end=on_task_end,
                    on_log=on_log,
                )

                wave_result = await execute_wave(opts)

                exec_logger.wave_ended(wave.name, wave_idx, passed=wave_result.passed)
                _flush_log()

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

            # Complete
            duration_ms = int((time.monotonic() - start_time) * 1000)
            execution.status = "completed" if all_passed else "failed"
            execution.completed_tasks = completed_count
            execution.finished_at = datetime.now(timezone.utc)
            await db.commit()

            # Finalize execution log
            exec_logger.execution_finished(all_passed=all_passed)
            _flush_log()

            await _emit_event(
                db, execution_id, "run_completed",
                payload={
                    "passed": all_passed,
                    "total_tasks": total_tasks,
                    "completed_tasks": completed_count,
                    "duration_ms": duration_ms,
                },
            )

        except asyncio.CancelledError:
            async with async_session() as db2:
                execution = await db2.get(Execution, execution_id)
                if execution:
                    execution.status = "cancelled"
                    execution.finished_at = datetime.now(timezone.utc)
                    await db2.commit()
            raise

        except Exception as e:
            async with async_session() as db2:
                execution = await db2.get(Execution, execution_id)
                if execution:
                    execution.status = "failed"
                    execution.finished_at = datetime.now(timezone.utc)
                    await db2.commit()
                await _emit_event(
                    db2, execution_id, "run_completed",
                    payload={"passed": False, "error": str(e)},
                )


async def _update_completed_count(execution_id: str, count: int) -> None:
    async with async_session() as db:
        execution = await db.get(Execution, execution_id)
        if execution:
            execution.completed_tasks = count
            await db.commit()
