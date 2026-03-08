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
) -> list[TaskResult]:
    """Execute tasks respecting DAG order.

    Tasks at the same level run concurrently. If a task fails,
    downstream dependents are skipped.
    """
    levels = build_dag(tasks)
    result_map: dict[str, TaskResult] = {}
    failed_ids: set[str] = set()

    def should_skip(task: Task) -> bool:
        return any(dep in failed_ids for dep in task.depends)

    for level in levels:
        level_results = await map_concurrent(
            level.tasks,
            max_concurrency,
            async_fn_factory(run_task, should_skip, failed_ids),
        )
        for result in level_results:
            result_map[result.id] = result

    return [result_map[t.id] for t in tasks]


def async_fn_factory(
    run_task: Callable[[Task], Awaitable[TaskResult]],
    should_skip: Callable[[Task], bool],
    failed_ids: set[str],
) -> Callable[[Task, int], Awaitable[TaskResult]]:
    async def fn(task: Task, _idx: int) -> TaskResult:
        if should_skip(task):
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
            return skipped

        result = await run_task(task)
        if result.exit_code != 0:
            failed_ids.add(task.id)
        return result

    return fn


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
