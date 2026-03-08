"""DAG scheduler — dependency resolution, topological ordering, and execution.

Validates task dependencies form a DAG (no cycles), builds topologically
sorted levels, and executes tasks level-by-level with parallelism within levels.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import TypeVar

from wave_server.engine.types import DAGLevel, Plan, Task, TaskResult

T = TypeVar("T")
R = TypeVar("R")


# ── Validation ─────────────────────────────────────────────────────


def validate_dag(tasks: list[Task]) -> tuple[bool, str | None]:
    """Validate that task dependencies form a valid DAG.

    Returns (valid, error_message).
    """
    task_ids = {t.id for t in tasks}

    for task in tasks:
        for dep in task.depends:
            if dep not in task_ids:
                return False, f'Task "{task.id}" depends on "{dep}" which does not exist'
        if task.id in task.depends:
            return False, f'Task "{task.id}" depends on itself'

    # Cycle detection via Kahn's algorithm
    in_degree: dict[str, int] = {}
    adjacency: dict[str, list[str]] = defaultdict(list)

    for task in tasks:
        in_degree[task.id] = len(task.depends)

    for task in tasks:
        for dep in task.depends:
            adjacency[dep].append(task.id)

    queue = [tid for tid, deg in in_degree.items() if deg == 0]
    sorted_count = 0

    while queue:
        tid = queue.pop(0)
        sorted_count += 1
        for dependent in adjacency.get(tid, []):
            in_degree[dependent] -= 1
            if in_degree[dependent] == 0:
                queue.append(dependent)

    if sorted_count != len(tasks):
        cyclic = [t.id for t in tasks if in_degree.get(t.id, 0) > 0]
        return False, f"Circular dependency detected among tasks: {', '.join(cyclic)}"

    return True, None


# ── Plan-Level Validation ──────────────────────────────────────────


def validate_plan(plan: Plan) -> tuple[bool, list[str]]:
    """Validate an entire plan's DAG structure.

    Returns (valid, list_of_errors).
    """
    errors: list[str] = []

    for wave in plan.waves:
        wave_label = f'Wave "{wave.name}"'

        # Collect all task IDs grouped by section
        section_tasks: dict[str, set[str]] = {}
        foundation_ids = {t.id for t in wave.foundation}
        section_tasks["foundation"] = foundation_ids

        for feature in wave.features:
            feature_ids = {t.id for t in feature.tasks}
            section_tasks[f"feature:{feature.name}"] = feature_ids

        integration_ids = {t.id for t in wave.integration}
        section_tasks["integration"] = integration_ids

        # Duplicate ID detection
        all_wave_ids: dict[str, str] = {}
        for section, ids in section_tasks.items():
            for tid in ids:
                if tid in all_wave_ids:
                    errors.append(
                        f'{wave_label}: Duplicate task ID "{tid}" — found in both {all_wave_ids[tid]} and {section}'
                    )
                all_wave_ids[tid] = section

        # Per-section validation + cross-section dependency check
        def validate_section(
            tasks: list[Task], section_label: str, section_ids: set[str]
        ):
            if tasks:
                valid, error = validate_dag(tasks)
                if not valid:
                    errors.append(f"{wave_label} {section_label}: {error}")

            for task in tasks:
                for dep in task.depends:
                    if dep not in section_ids and dep in all_wave_ids:
                        dep_section = all_wave_ids[dep]
                        errors.append(
                            f'{wave_label} {section_label}: Task "{task.id}" depends on "{dep}" which is in {dep_section}. '
                            f"Dependencies must be within the same section — the executor handles cross-section ordering automatically."
                        )

        validate_section(wave.foundation, "foundation", foundation_ids)
        for feature in wave.features:
            feature_ids = section_tasks[f"feature:{feature.name}"]
            validate_section(
                feature.tasks, f'feature "{feature.name}"', feature_ids
            )
        validate_section(wave.integration, "integration", integration_ids)

        # Feature file overlap detection
        file_ownership: dict[str, list[str]] = defaultdict(list)
        for feature in wave.features:
            feature_files: set[str] = set()
            for task in feature.tasks:
                feature_files.update(task.files)
            feature_files.update(feature.files)
            for f in feature_files:
                file_ownership[f].append(feature.name)
        for f, features in file_ownership.items():
            if len(features) > 1:
                errors.append(
                    f'{wave_label}: File "{f}" is written by multiple parallel features: {", ".join(features)}. '
                    f"Move shared files to Foundation or split into separate waves."
                )

    # v2 required sections — these drive token optimization
    if not plan.project_structure:
        errors.append(
            "Missing '## Project Structure' section. "
            "This section is required — it gives agents the file layout so they don't waste tokens exploring."
        )
    if not plan.data_schemas:
        errors.append(
            "Missing '## Data Schemas' section. "
            "This section is required — it gives agents authoritative type/model names."
        )
    if not plan.waves:
        errors.append("No waves found in plan.")

    return len(errors) == 0, errors


# ── Build DAG Levels ───────────────────────────────────────────────


def build_dag(tasks: list[Task]) -> list[DAGLevel]:
    """Build topologically sorted levels from tasks."""
    if not tasks:
        return []

    assigned: dict[str, int] = {}
    levels: list[DAGLevel] = []
    remaining = list(tasks)
    level_num = 0

    while remaining:
        this_level: list[Task] = []
        next_remaining: list[Task] = []

        for task in remaining:
            if all(d in assigned for d in task.depends):
                this_level.append(task)
            else:
                next_remaining.append(task)

        if not this_level:
            break

        for task in this_level:
            assigned[task.id] = level_num

        levels.append(DAGLevel(tasks=this_level, parallel=len(this_level) > 1))
        remaining = next_remaining
        level_num += 1

    return levels


def get_dag_levels(tasks: list[Task]) -> list[DAGLevel]:
    """Return DAG levels for level-by-level execution.

    Alias for build_dag — provided for semantic clarity in the feature
    executor which needs level-by-level control for sub-worktree hooks.
    """
    return build_dag(tasks)


# ── Concurrent Execution Helper ────────────────────────────────────


async def map_concurrent(
    items: list[T],
    concurrency: int,
    fn: Callable[[T, int], Awaitable[R]],
) -> list[R]:
    """Run items concurrently with a limit on simultaneous operations."""
    results: list[R | None] = [None] * len(items)
    next_idx = 0
    lock = asyncio.Lock()

    async def worker():
        nonlocal next_idx
        while True:
            async with lock:
                idx = next_idx
                next_idx += 1
            if idx >= len(items):
                return
            results[idx] = await fn(items[idx], idx)

    workers = [
        asyncio.create_task(worker())
        for _ in range(min(concurrency, len(items)))
    ]
    await asyncio.gather(*workers)
    return results  # type: ignore[return-value]


# ── DAG Execution ──────────────────────────────────────────────────


async def execute_dag(
    tasks: list[Task],
    run_task: Callable[[Task], Awaitable[TaskResult]],
    max_concurrency: int,
    *,
    semaphore: asyncio.Semaphore | None = None,
) -> list[TaskResult]:
    """Execute tasks respecting DAG order with ready-queue scheduling.

    Instead of processing level-by-level (where ALL tasks at level N must
    finish before ANY task at level N+1 starts), tasks are launched as soon
    as all their dependencies have completed.  This maximises parallelism
    within the concurrency limit.

    If a *semaphore* is provided it is used for concurrency control instead
    of creating a local one.  This allows a single semaphore to be shared
    across foundation, features, and integration phases so the global
    ``max_concurrency`` limit is truly enforced.

    If a task fails, downstream dependents are skipped.
    """
    if not tasks:
        return []

    task_map = {t.id: t for t in tasks}
    result_map: dict[str, TaskResult] = {}
    failed_ids: set[str] = set()
    completed_ids: set[str] = set()

    # Reverse dependency map: task_id → tasks that list it as a dependency
    dependents: dict[str, set[str]] = defaultdict(set)
    for task in tasks:
        for dep in task.depends:
            dependents[dep].add(task.id)

    # Number of unsatisfied dependencies per task
    remaining_deps: dict[str, int] = {t.id: len(t.depends) for t in tasks}

    sem = semaphore or asyncio.Semaphore(max_concurrency)
    tasks_left = len(tasks)
    all_done = asyncio.Event()
    in_flight: set[asyncio.Task[None]] = set()
    fatal_exception: BaseException | None = None

    def _launch(task: Task) -> None:
        """Schedule a task for execution."""

        async def _run() -> None:
            nonlocal tasks_left, fatal_exception

            try:
                # Skip if any dependency failed
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
                else:
                    async with sem:
                        result = await run_task(task)
                    result_map[task.id] = result
                    if result.exit_code != 0:
                        failed_ids.add(task.id)
            except Exception as exc:
                # Fatal: store exception.  Don't enqueue dependents —
                # running tasks will finish naturally, then we re-raise.
                if fatal_exception is None:
                    fatal_exception = exc
                failed_ids.add(task.id)

            completed_ids.add(task.id)
            tasks_left -= 1

            if fatal_exception is not None:
                # Aborting — signal done when no more tasks are in flight
                # (the done_callback hasn't fired yet for *this* task,
                # so in_flight still includes us — check for <= 1)
                if len(in_flight) <= 1:
                    all_done.set()
            else:
                # Enqueue dependents whose dependencies are now fully satisfied
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

    # Seed with root tasks (no dependencies)
    for task in tasks:
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

    return [result_map[t.id] for t in tasks]


def compute_dirty_closure(plan: Plan, rerun_ids: set[str], cascade: bool = True) -> set[str]:
    """Compute the full set of task IDs that must be re-executed.

    Args:
        plan: The parsed execution plan.
        rerun_ids: Task IDs explicitly selected for rerun.
        cascade: If True, include all transitive downstream dependents
                 and apply implicit cross-section cascading.
                 If False, return only the selected task IDs.

    Returns:
        The set of task IDs that should NOT be skipped (i.e. must run).
    """
    if not cascade:
        return set(rerun_ids)

    dirty: set[str] = set(rerun_ids)

    for wave in plan.waves:
        # Collect all tasks in this wave by section
        all_tasks: list[Task] = []
        foundation_ids: set[str] = set()
        feature_ids: set[str] = set()
        integration_ids: set[str] = set()

        for t in wave.foundation:
            all_tasks.append(t)
            foundation_ids.add(t.id)
        for feature in wave.features:
            for t in feature.tasks:
                all_tasks.append(t)
                feature_ids.add(t.id)
        for t in wave.integration:
            all_tasks.append(t)
            integration_ids.add(t.id)

        # Build forward adjacency (task -> its dependents)
        forward: dict[str, list[str]] = defaultdict(list)
        for t in all_tasks:
            for dep in t.depends:
                forward[dep].append(t.id)

        # Walk explicit DAG edges forward from dirty tasks in this wave
        wave_ids = {t.id for t in all_tasks}
        queue = [tid for tid in dirty if tid in wave_ids]
        visited: set[str] = set(queue)
        while queue:
            current = queue.pop(0)
            for dependent in forward.get(current, []):
                if dependent not in visited:
                    visited.add(dependent)
                    dirty.add(dependent)
                    queue.append(dependent)

        # Implicit cross-section cascading:
        # - dirty foundation task → all features + integration dirty
        # - dirty feature task → all integration dirty
        if dirty & foundation_ids:
            dirty |= feature_ids | integration_ids
        elif dirty & feature_ids:
            dirty |= integration_ids

    return dirty
