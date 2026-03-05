"""Feature executor — runs a single feature's task DAG with sub-worktree isolation.

For parallel tasks at the same DAG level:
  - Creates sub-worktrees branching from the feature branch
  - Runs tasks in parallel, each in its own sub-worktree
  - Merges sub-worktrees back into the feature branch

For sequential tasks (single task at a DAG level):
  - Runs directly in the feature worktree (no sub-worktree overhead)
"""

from __future__ import annotations

import inspect
import time
from collections.abc import Callable
from typing import Any

from wave_server.engine.dag import build_dag, map_concurrent
from wave_server.engine.git_worktree import (
    commit_task_output,
    create_sub_worktrees,
    merge_sub_worktrees,
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
) -> FeatureResult:
    """Execute a feature's task DAG with sub-worktree isolation for parallel tasks."""
    skip_ids = skip_task_ids or set()
    task_results: list[TaskResult] = []
    failed_ids: set[str] = set()

    feature_cwd = feature_worktree.dir if feature_worktree else cwd
    levels = build_dag(feature.tasks)

    for level in levels:
        if not level.tasks:
            continue

        # Separate runnable vs skipped (dependency failed)
        runnable_tasks = [
            t for t in level.tasks if not any(d in failed_ids for d in t.depends)
        ]
        skipped_tasks = [
            t for t in level.tasks if any(d in failed_ids for d in t.depends)
        ]

        # Mark skipped tasks
        for task in skipped_tasks:
            skipped = TaskResult(
                id=task.id,
                title=task.title,
                agent=task.agent,
                exit_code=-1,
                output="Skipped: dependency failed",
                stderr="",
                duration_ms=0,
            )
            failed_ids.add(task.id)
            task_results.append(skipped)
            await _call(on_task_end, task, skipped)

        if not runnable_tasks:
            continue

        # Decide isolation strategy: sub-worktrees for parallel tasks
        use_sub_worktrees = len(runnable_tasks) > 1 and feature_worktree is not None
        sub_worktrees: list[SubWorktree] = []

        if use_sub_worktrees:
            sub_worktrees = await create_sub_worktrees(
                feature_worktree, wave_num, [t.id for t in runnable_tasks]
            )
            # If creation failed, sub_worktrees is empty → fall back to sequential

        actually_parallel = len(sub_worktrees) == len(runnable_tasks)
        sub_wt_map = {sw.task_id: sw for sw in sub_worktrees}

        async def run_task(task: Task, _idx: int) -> TaskResult:
            if task.id in skip_ids:
                skipped = TaskResult(
                    id=task.id,
                    title=task.title,
                    agent=task.agent,
                    exit_code=0,
                    output="Resumed — already completed in previous run",
                    stderr="",
                    duration_ms=0,
                )
                await _call(on_task_start, task)
                await _call(on_task_end, task, skipped)
                return skipped

            await _call(on_task_start, task)
            start = time.monotonic()

            # Determine working directory
            task_cwd = feature_cwd
            if actually_parallel:
                sw = sub_wt_map.get(task.id)
                if sw:
                    task_cwd = sw.dir

            from wave_server.engine.wave_executor import _build_task_prompt

            prompt = _build_task_prompt(task, spec_content, data_schemas, project_context)

            # Add worktree context to prompt
            if feature_worktree is not None:
                prompt += (
                    "\n\nNote: You are working in an isolated git worktree. "
                    "Use relative paths only. Do NOT run git checkout or git branch commands."
                )

            # Resolve model: agent-specific override > execution default
            task_model = (agent_models or {}).get(task.agent) or model or None

            config = RunnerConfig(task_id=task.id, prompt=prompt, cwd=task_cwd, env=env, model=task_model)
            runner_result = await runner.spawn(config)
            elapsed_ms = int((time.monotonic() - start) * 1000)
            output = runner.extract_final_output(runner_result.stdout)

            result = TaskResult(
                id=task.id,
                title=task.title,
                agent=task.agent,
                exit_code=runner_result.exit_code,
                output=output if not runner_result.timed_out else f"Task timed out\n{output}",
                stderr=runner_result.stderr,
                duration_ms=elapsed_ms,
                stdout=runner_result.stdout,
                timed_out=runner_result.timed_out,
            )

            # Per-task commit for sequential tasks in feature worktree (or base branch).
            # Sub-worktree tasks are committed during merge_sub_worktrees instead.
            should_commit = feature_worktree is not None or auto_commit
            if result.exit_code == 0 and not actually_parallel and should_commit:
                committed = await commit_task_output(
                    task_cwd, task.id, task.title, task.agent
                )
                if committed:
                    await _call(
                        on_log,
                        f"   📌 Committed: {task.id} [{task.agent}] — {task.title}",
                    )

            if result.exit_code != 0:
                failed_ids.add(task.id)

            await _call(on_task_end, task, result)
            return result

        level_results = await map_concurrent(
            runnable_tasks,
            max_concurrency if actually_parallel else 1,
            run_task,
        )
        task_results.extend(level_results)

        # Merge sub-worktrees back into feature branch
        if actually_parallel and sub_worktrees:
            merge_task_results = [
                {
                    "task_id": r.id,
                    "exit_code": r.exit_code,
                    "title": r.title,
                    "agent": r.agent,
                }
                for r in level_results
            ]
            await merge_sub_worktrees(
                feature_worktree, sub_worktrees, merge_task_results, runner
            )

    all_passed = all(r.exit_code == 0 for r in task_results)

    return FeatureResult(
        name=feature.name,
        branch=feature_worktree.branch if feature_worktree else "",
        task_results=task_results,
        passed=all_passed,
    )
