"""Feature executor — runs a single feature's task DAG with ready-queue scheduling.

Uses a ready-queue scheduler instead of level-by-level execution: a task
starts as soon as ALL its dependencies have completed, without waiting for
unrelated sibling tasks at the same topological level.

When parallel execution is possible (DAG has width > 1) and a feature
worktree is available, each task gets its own sub-worktree for git
isolation.  Sub-worktrees are created just before a task starts and
merged back immediately after it completes, so downstream dependents
always see the latest state.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections import defaultdict
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from wave_server.engine.dag import build_dag
from wave_server.engine.enforcement import is_verifier_failure
from wave_server.engine.verify_fix import attempt_fix_and_reverify
from wave_server.engine.git_worktree import (
    cleanup_single_sub_worktree,
    commit_task_output,
    create_single_sub_worktree,
    merge_single_sub_worktree,
)
from wave_server.engine.runner import AgentRunner
from wave_server.engine.types import (
    Feature,
    FeatureResult,
    FeatureWorktree,
    RunnerConfig,
    SubWorktree,
    Task,
    TaskResult,
)


async def _call(fn: Callable | None, *args: Any) -> None:
    """Call a callback, awaiting it if it's async."""
    if fn is None:
        return
    result = fn(*args)
    if inspect.isawaitable(result):
        await result


async def execute_feature(
    feature: Feature,
    runner: AgentRunner,
    spec_content: str = "",
    data_schemas: str = "",
    project_structure: str = "",
    environment: str = "",
    project_context: str = "",
    cwd: str = ".",
    max_concurrency: int = 4,
    skip_task_ids: set[str] | None = None,
    feature_worktree: FeatureWorktree | None = None,
    wave_num: int = 1,
    env: dict[str, str] | None = None,
    auto_commit: bool = False,
    model: str | None = None,
    agent_models: dict[str, str] | None = None,
    on_task_start: Callable[[Task], Any] | None = None,
    on_task_end: Callable[[Task, TaskResult], Any] | None = None,
    on_log: Callable[[str], Any] | None = None,
    semaphore: asyncio.Semaphore | None = None,
) -> FeatureResult:
    """Execute a feature's task DAG with ready-queue scheduling.

    Tasks start as soon as their dependencies are satisfied rather than
    waiting for all tasks at the same topological level to complete.

    When git isolation is needed (parallel paths + feature worktree),
    each task gets a per-task sub-worktree that is created on demand and
    merged back immediately on completion.
    """
    skip_ids = skip_task_ids or set()
    feature_cwd = feature_worktree.dir if feature_worktree else cwd

    if not feature.tasks:
        return FeatureResult(
            name=feature.name,
            branch=feature_worktree.branch if feature_worktree else "",
            task_results=[],
            passed=True,
        )

    # ── Determine whether sub-worktrees are needed ────────────

    levels = build_dag(feature.tasks)
    has_parallelism = any(len(level.tasks) > 1 for level in levels)
    use_sub_wt = has_parallelism and feature_worktree is not None

    # Global concurrency gate — shared across all features so that
    # ``max_concurrency`` is truly the system-wide ceiling.
    shared_sem = semaphore or asyncio.Semaphore(max_concurrency)

    # When tasks share a working directory (no sub-worktrees) we need
    # an additional lock so only one task touches the directory at a
    # time.  The lock is acquired *before* the shared semaphore so we
    # don't hold a global slot while waiting for directory access.
    dir_lock: asyncio.Lock | None = None if use_sub_wt else asyncio.Lock()

    # ── Ready-queue state ─────────────────────────────────────

    task_map: dict[str, Task] = {t.id: t for t in feature.tasks}
    result_map: dict[str, TaskResult] = {}
    failed_ids: set[str] = set()
    completed_ids: set[str] = set()

    dependents: dict[str, set[str]] = defaultdict(set)
    for task in feature.tasks:
        for dep in task.depends:
            dependents[dep].add(task.id)

    remaining_deps: dict[str, int] = {t.id: len(t.depends) for t in feature.tasks}
    git_lock = asyncio.Lock()  # serialise sub-worktree create / merge
    tasks_left = len(feature.tasks)
    all_done = asyncio.Event()
    in_flight: set[asyncio.Task[None]] = set()
    fatal_exception: BaseException | None = None

    # ── Per-task launcher ─────────────────────────────────────

    def _launch(task: Task) -> None:  # noqa: C901 — complexity from lifecycle mgmt
        async def _run() -> None:
            nonlocal tasks_left, fatal_exception

            sw: SubWorktree | None = None
            task_cwd = feature_cwd

            try:
                # ── Skip: dependency failed ───────────────
                if any(dep in failed_ids for dep in task.depends):
                    result = TaskResult(
                        id=task.id,
                        title=task.title,
                        agent=task.agent,
                        exit_code=-1,
                        output="Skipped: dependency failed",
                        stderr="",
                        duration_ms=0,
                    )
                    failed_ids.add(task.id)
                    result_map[task.id] = result
                    await _call(on_task_end, task, result)
                    return

                # ── Skip: already completed (resume) ──────
                if task.id in skip_ids:
                    result = TaskResult(
                        id=task.id,
                        title=task.title,
                        agent=task.agent,
                        exit_code=0,
                        output="Resumed — already completed in previous run",
                        stderr="",
                        duration_ms=0,
                    )
                    await _call(on_task_start, task)
                    await _call(on_task_end, task, result)
                    result_map[task.id] = result
                    return

                # ── Acquire concurrency slot ──────────────
                # dir_lock (if set) serialises access to the shared
                # working directory; shared_sem enforces the global
                # max_concurrency ceiling.
                @asynccontextmanager
                async def _concurrency_gate() -> AsyncIterator[None]:
                    if dir_lock:
                        async with dir_lock:
                            async with shared_sem:
                                yield
                    else:
                        async with shared_sem:
                            yield

                async with _concurrency_gate():
                    # Create sub-worktree (inside gate so we don't
                    # create more worktrees than we can run)
                    if use_sub_wt:
                        async with git_lock:
                            sw = await create_single_sub_worktree(
                                feature_worktree,
                                wave_num,
                                task.id,  # type: ignore[arg-type]
                            )
                        if sw:
                            task_cwd = sw.dir
                        # If creation failed, fall through and run in
                        # feature_cwd.  The semaphore prevents concurrent
                        # access in that case.

                    # ── Run the task ──────────────────────
                    await _call(on_task_start, task)
                    start = time.monotonic()

                    from wave_server.engine.wave_executor import _build_task_prompt

                    prompt = _build_task_prompt(
                        task,
                        spec_content,
                        data_schemas,
                        project_structure,
                        environment,
                        project_context,
                    )

                    if feature_worktree is not None:
                        prompt += (
                            "\n\nNote: You are working in an isolated git worktree. "
                            "Use relative paths only. Do NOT run git checkout or git branch commands."
                        )

                    task_model = (agent_models or {}).get(task.agent) or model or None
                    config = RunnerConfig(
                        task_id=task.id,
                        prompt=prompt,
                        cwd=task_cwd,
                        env=env,
                        model=task_model,
                    )

                    runner_result = await runner.spawn(config)
                    elapsed_ms = int((time.monotonic() - start) * 1000)
                    output = runner.extract_final_output(runner_result.stdout)

                    result = TaskResult(
                        id=task.id,
                        title=task.title,
                        agent=task.agent,
                        exit_code=runner_result.exit_code,
                        output=output
                        if not runner_result.timed_out
                        else f"Task timed out\n{output}",
                        stderr=runner_result.stderr,
                        duration_ms=elapsed_ms,
                        stdout=runner_result.stdout,
                        timed_out=runner_result.timed_out,
                    )

                    # Wave-verifier tasks may exit 0 but report failure
                    # in their output JSON.  Attempt fix-verify loop
                    # before giving up.
                    if task.agent == "wave-verifier" and result.exit_code == 0:
                        if is_verifier_failure(result.output or ""):
                            await _call(
                                on_log,
                                f"   ⚠️  Verifier {task.id} reported failure — attempting fix",
                            )
                            from wave_server.engine.wave_executor import (
                                _build_task_prompt,
                            )

                            verifier_prompt = _build_task_prompt(
                                task,
                                spec_content,
                                data_schemas,
                                project_structure,
                                environment,
                                project_context,
                            )
                            fixed = await attempt_fix_and_reverify(
                                verifier_task=task,
                                verifier_output=result.output or "",
                                verifier_prompt=verifier_prompt,
                                runner=runner,
                                cwd=task_cwd,
                                env=env,
                                model=model,
                                agent_models=agent_models,
                                max_attempts=2,
                                auto_commit=feature_worktree is not None or auto_commit,
                                on_log=on_log,
                            )
                            if fixed:
                                result = fixed
                            else:
                                result = TaskResult(
                                    id=result.id,
                                    title=result.title,
                                    agent=result.agent,
                                    exit_code=1,
                                    output=result.output,
                                    stderr="Verification failed after fix attempts exhausted",
                                    duration_ms=result.duration_ms,
                                    stdout=result.stdout,
                                    timed_out=result.timed_out,
                                )

                    if result.exit_code != 0:
                        failed_ids.add(task.id)

                    # ── Post-task git operations ──────────
                    if sw:
                        async with git_lock:
                            if result.exit_code == 0:
                                merge_result = await merge_single_sub_worktree(
                                    feature_worktree,  # type: ignore[arg-type]
                                    sw,
                                    task.id,
                                    task.title,
                                    task.agent,
                                    runner,
                                    on_log=on_log,
                                )
                                if merge_result.had_changes and merge_result.success:
                                    await _call(
                                        on_log,
                                        f"   📌 Committed: {task.id} [{task.agent}] — {task.title}",
                                    )
                            else:
                                await cleanup_single_sub_worktree(
                                    feature_worktree.repo_root,
                                    sw,  # type: ignore[union-attr]
                                )
                    else:
                        # No sub-worktree — commit directly in feature dir
                        should_commit = feature_worktree is not None or auto_commit
                        if result.exit_code == 0 and should_commit:
                            committed = await commit_task_output(
                                task_cwd, task.id, task.title, task.agent
                            )
                            if committed:
                                await _call(
                                    on_log,
                                    f"   📌 Committed: {task.id} [{task.agent}] — {task.title}",
                                )

                    result_map[task.id] = result
                    await _call(on_task_end, task, result)

            except Exception as exc:
                # Best-effort cleanup of sub-worktree
                if sw:
                    try:
                        async with git_lock:
                            await cleanup_single_sub_worktree(
                                feature_worktree.repo_root,
                                sw,  # type: ignore[union-attr]
                            )
                    except Exception:
                        pass

                # Store as fatal — running tasks will finish naturally,
                # then we re-raise.  Don't cancel to avoid interrupting
                # DB operations in callbacks.
                if fatal_exception is None:
                    fatal_exception = exc

            finally:
                completed_ids.add(task.id)
                tasks_left -= 1

                if fatal_exception is not None:
                    # Aborting — signal done when no more tasks are in flight
                    if len(in_flight) <= 1:
                        all_done.set()
                else:
                    for dep_id in dependents.get(task.id, set()):
                        if dep_id not in completed_ids:
                            remaining_deps[dep_id] -= 1
                            if remaining_deps[dep_id] == 0:
                                _launch(task_map[dep_id])

                    if tasks_left == 0:
                        all_done.set()

        aio_task = asyncio.create_task(_run())
        in_flight.add(aio_task)
        aio_task.add_done_callback(in_flight.discard)

    # ── Seed with root tasks ──────────────────────────────────

    for task in feature.tasks:
        if remaining_deps[task.id] == 0:
            _launch(task)

    await all_done.wait()

    # Wait for any remaining in-flight tasks to finish before re-raising.
    # We do NOT cancel them — cancellation sends CancelledError which can
    # interrupt DB operations in callbacks, corrupting shared connections.
    if in_flight:
        await asyncio.gather(*in_flight, return_exceptions=True)
    if fatal_exception is not None:
        raise fatal_exception

    # ── Collect results in original task order ────────────────

    task_results = [result_map[t.id] for t in feature.tasks if t.id in result_map]
    all_passed = all(r.exit_code == 0 for r in task_results)

    return FeatureResult(
        name=feature.name,
        branch=feature_worktree.branch if feature_worktree else "",
        task_results=task_results,
        passed=all_passed,
    )
