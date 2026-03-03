"""Wave executor — runs a complete wave: foundation -> features -> merge -> integration.

Ported from TypeScript wave-executor.ts. Simplified for server context:
- Events are emitted via callbacks (server inserts into DB)
- Git worktree operations delegated to git_worktree module
- Runner is pluggable via AgentRunner protocol
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from wave_server.engine.dag import execute_dag, map_concurrent
from wave_server.engine.runner import AgentRunner
from wave_server.engine.types import (
    Feature,
    FeatureResult,
    MergeResult,
    ProgressUpdate,
    Task,
    TaskResult,
    Wave,
    WaveResult,
)


@dataclass
class WaveExecutorOptions:
    wave: Wave
    wave_num: int
    runner: AgentRunner
    spec_content: str = ""
    data_schemas: str = ""
    cwd: str = "."
    max_concurrency: int = 4
    skip_task_ids: set[str] = field(default_factory=set)

    # Callbacks
    on_progress: Callable[[ProgressUpdate], None] | None = None
    on_task_start: Callable[[str, Task], None] | None = None
    on_task_end: Callable[[str, Task, TaskResult], None] | None = None
    on_merge_result: Callable[[MergeResult], None] | None = None
    on_log: Callable[[str], None] | None = None


async def execute_wave(opts: WaveExecutorOptions) -> WaveResult:
    """Execute a complete wave: foundation -> features -> merge -> integration."""
    wave = opts.wave
    foundation_results: list[TaskResult] = []
    feature_results: list[FeatureResult] = []
    integration_results: list[TaskResult] = []

    async def run_task_with_runner(
        task: Task, phase: str
    ) -> TaskResult:
        """Run a single task using the configured runner."""
        # Skip already-completed tasks
        if task.id in opts.skip_task_ids:
            skipped = TaskResult(
                id=task.id,
                title=task.title,
                agent=task.agent,
                exit_code=0,
                output="Resumed — already completed in previous run",
                stderr="",
                duration_ms=0,
            )
            if opts.on_task_start:
                opts.on_task_start(phase, task)
            if opts.on_task_end:
                opts.on_task_end(phase, task, skipped)
            return skipped

        if opts.on_task_start:
            opts.on_task_start(phase, task)

        start = time.monotonic()

        # Build prompt for the agent
        prompt = _build_task_prompt(task, opts.spec_content, opts.data_schemas)

        from wave_server.engine.types import RunnerConfig

        config = RunnerConfig(
            task_id=task.id,
            prompt=prompt,
            cwd=opts.cwd,
        )

        runner_result = await opts.runner.spawn(config)
        elapsed_ms = int((time.monotonic() - start) * 1000)

        output = opts.runner.extract_final_output(runner_result.stdout)

        result = TaskResult(
            id=task.id,
            title=task.title,
            agent=task.agent,
            exit_code=runner_result.exit_code,
            output=output if not runner_result.timed_out else f"Task timed out\n{output}",
            stderr=runner_result.stderr,
            duration_ms=elapsed_ms,
            timed_out=runner_result.timed_out,
        )

        if opts.on_task_end:
            opts.on_task_end(phase, task, result)

        return result

    # ── 1. Foundation Phase ─────────────────────────────────────

    if wave.foundation:
        if opts.on_progress:
            opts.on_progress(
                ProgressUpdate(
                    phase="foundation",
                    current_tasks=[{"id": t.id, "status": "pending"} for t in wave.foundation],
                )
            )
        if opts.on_log:
            opts.on_log("### Foundation")

        f_results = await execute_dag(
            wave.foundation,
            lambda task: run_task_with_runner(task, "foundation"),
            opts.max_concurrency,
        )
        foundation_results.extend(f_results)

        if any(r.exit_code != 0 for r in f_results):
            if opts.on_log:
                opts.on_log("Foundation FAILED — skipping features and integration")
            return WaveResult(
                wave=wave.name,
                foundation_results=foundation_results,
                feature_results=feature_results,
                integration_results=integration_results,
                passed=False,
            )

    # ── 2. Feature Phase ───────────────────────────────────────

    if wave.features:
        if opts.on_progress:
            opts.on_progress(
                ProgressUpdate(
                    phase="features",
                    features=[{"name": f.name, "status": "pending"} for f in wave.features],
                )
            )
        if opts.on_log:
            opts.on_log("### Features")

        per_feature_concurrency = max(
            2, opts.max_concurrency // len(wave.features)
        )

        async def run_feature(feature: Feature, idx: int) -> FeatureResult:
            task_results: list[TaskResult] = []
            for task in feature.tasks:
                result = await run_task_with_runner(task, f"feature:{feature.name}")
                task_results.append(result)
                if result.exit_code != 0:
                    break  # Stop feature on first failure

            all_passed = all(r.exit_code == 0 for r in task_results)
            return FeatureResult(
                name=feature.name,
                branch="",
                task_results=task_results,
                passed=all_passed,
            )

        # For now, run features sequentially (git worktree isolation is Phase 2 task 13)
        # When git worktree support is added, features with isolation can run in parallel
        is_single_default = len(wave.features) == 1 and wave.features[0].name == "default"

        f_results = await map_concurrent(
            wave.features,
            1 if is_single_default else len(wave.features),
            run_feature,
        )
        feature_results.extend(f_results)

        if any(not r.passed for r in f_results):
            if opts.on_log:
                opts.on_log("One or more features failed — skipping integration")
            return WaveResult(
                wave=wave.name,
                foundation_results=foundation_results,
                feature_results=feature_results,
                integration_results=integration_results,
                passed=False,
            )

    # ── 3. Integration Phase ───────────────────────────────────

    if wave.integration:
        if opts.on_progress:
            opts.on_progress(
                ProgressUpdate(
                    phase="integration",
                    current_tasks=[{"id": t.id, "status": "pending"} for t in wave.integration],
                )
            )
        if opts.on_log:
            opts.on_log("### Integration")

        i_results = await execute_dag(
            wave.integration,
            lambda task: run_task_with_runner(task, "integration"),
            opts.max_concurrency,
        )
        integration_results.extend(i_results)

    passed = (
        all(r.exit_code == 0 for r in foundation_results)
        and all(r.passed for r in feature_results)
        and all(r.exit_code == 0 for r in integration_results)
    )

    return WaveResult(
        wave=wave.name,
        foundation_results=foundation_results,
        feature_results=feature_results,
        integration_results=integration_results,
        passed=passed,
    )


def _build_task_prompt(task: Task, spec_content: str, data_schemas: str) -> str:
    """Build the prompt sent to the agent subprocess."""
    schemas_block = (
        f"\n## Data Schemas (authoritative — use these exact names)\n{data_schemas}\n"
        if data_schemas
        else ""
    )

    if task.agent == "wave-verifier":
        return f"""You are verifying completed work.
{schemas_block}
## Your Task
**{task.id}: {task.title}**
{f"Files to check: {', '.join(task.files)}" if task.files else ""}

{task.description}

IMPORTANT — verify in this order:
1. File existence — check that required files exist
2. Syntax/compilation — run the compiler/linter
3. Tests — run the test suite
4. Completeness — verify implementation matches task descriptions
- Do NOT modify any files"""
    elif task.agent == "test-writer":
        return f"""You are writing tests.
{schemas_block}
## Your Task
**{task.id}: {task.title}**
Files: {', '.join(task.files)}

{task.description}

IMPORTANT:
- Only create/modify TEST files listed for this task
- Follow existing test patterns
- Use exact names from Data Schemas above"""
    else:
        test_context = (
            f"\nTests to satisfy: {', '.join(task.test_files)}\nYour implementation MUST make these tests pass."
            if task.test_files
            else ""
        )
        return f"""You are implementing code.
{schemas_block}
## Your Task
**{task.id}: {task.title}**
Files: {', '.join(task.files)}{test_context}

{task.description}

IMPORTANT:
- Only modify files listed for this task
- Follow the spec requirements exactly
- Use exact names from Data Schemas above"""
