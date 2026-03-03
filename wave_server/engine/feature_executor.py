"""Feature executor — runs a single feature's task DAG.

Simplified for server context. In the full implementation (with git worktree
isolation), parallel tasks within a feature use sub-worktrees. The server
version runs tasks through the DAG sequentially within each feature,
while features themselves can run in parallel via the wave executor.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from wave_server.engine.dag import build_dag, execute_dag, map_concurrent
from wave_server.engine.runner import AgentRunner
from wave_server.engine.types import (
    Feature,
    FeatureResult,
    FeatureWorktree,
    RunnerConfig,
    Task,
    TaskResult,
)


async def execute_feature(
    feature: Feature,
    runner: AgentRunner,
    spec_content: str = "",
    data_schemas: str = "",
    cwd: str = ".",
    max_concurrency: int = 4,
    skip_task_ids: set[str] | None = None,
    on_task_start: Callable[[Task], None] | None = None,
    on_task_end: Callable[[Task, TaskResult], None] | None = None,
    on_log: Callable[[str], None] | None = None,
) -> FeatureResult:
    """Execute a feature's task DAG."""
    skip_ids = skip_task_ids or set()
    task_results: list[TaskResult] = []

    async def run_task(task: Task) -> TaskResult:
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
            if on_task_start:
                on_task_start(task)
            if on_task_end:
                on_task_end(task, skipped)
            return skipped

        if on_task_start:
            on_task_start(task)

        start = time.monotonic()

        from wave_server.engine.wave_executor import _build_task_prompt

        prompt = _build_task_prompt(task, spec_content, data_schemas)
        config = RunnerConfig(task_id=task.id, prompt=prompt, cwd=cwd)
        runner_result = await runner.spawn(config)
        elapsed_ms = int((time.monotonic() - start) * 1000)
        output = runner.extract_final_output(runner_result.stdout)

        result = TaskResult(
            id=task.id,
            title=task.title,
            agent=task.agent,
            exit_code=runner_result.exit_code,
            output=output,
            stderr=runner_result.stderr,
            duration_ms=elapsed_ms,
            timed_out=runner_result.timed_out,
        )

        if on_task_end:
            on_task_end(task, result)

        return result

    results = await execute_dag(feature.tasks, run_task, max_concurrency)
    task_results.extend(results)

    all_passed = all(r.exit_code == 0 for r in task_results)

    return FeatureResult(
        name=feature.name,
        branch="",
        task_results=task_results,
        passed=all_passed,
    )
