import asyncio

import pytest

from wave_server.engine.dag import (
    build_dag,
    execute_dag,
    map_concurrent,
    validate_dag,
    validate_plan,
)
from wave_server.engine.types import Feature, Plan, Task, TaskResult, Wave


def _task(id: str, depends: list[str] | None = None, files: list[str] | None = None) -> Task:
    return Task(id=id, title=f"Task {id}", depends=depends or [], files=files or [])


# ── validate_dag ───────────────────────────────────────────────


def test_validate_dag_valid():
    tasks = [_task("a"), _task("b", ["a"]), _task("c", ["a", "b"])]
    valid, error = validate_dag(tasks)
    assert valid
    assert error is None


def test_validate_dag_missing_dep():
    tasks = [_task("a", ["nonexistent"])]
    valid, error = validate_dag(tasks)
    assert not valid
    assert "nonexistent" in error


def test_validate_dag_self_dep():
    tasks = [_task("a", ["a"])]
    valid, error = validate_dag(tasks)
    assert not valid
    assert "depends on itself" in error


def test_validate_dag_cycle():
    tasks = [_task("a", ["b"]), _task("b", ["a"])]
    valid, error = validate_dag(tasks)
    assert not valid
    assert "Circular" in error


# ── build_dag ──────────────────────────────────────────────────


def test_build_dag_empty():
    assert build_dag([]) == []


def test_build_dag_linear():
    tasks = [_task("a"), _task("b", ["a"]), _task("c", ["b"])]
    levels = build_dag(tasks)
    assert len(levels) == 3
    assert [l.tasks[0].id for l in levels] == ["a", "b", "c"]
    assert not levels[0].parallel


def test_build_dag_parallel():
    tasks = [_task("a"), _task("b"), _task("c", ["a", "b"])]
    levels = build_dag(tasks)
    assert len(levels) == 2
    assert levels[0].parallel  # a and b in parallel
    assert len(levels[0].tasks) == 2
    assert levels[1].tasks[0].id == "c"


# ── validate_plan ──────────────────────────────────────────────


def test_validate_plan_valid():
    plan = Plan(waves=[
        Wave(
            name="W1",
            foundation=[_task("f1")],
            features=[Feature(name="feat", tasks=[_task("t1"), _task("t2", ["t1"])])],
            integration=[_task("i1")],
        )
    ])
    valid, errors = validate_plan(plan)
    assert valid
    assert errors == []


def test_validate_plan_cross_section_dep():
    plan = Plan(waves=[
        Wave(
            name="W1",
            foundation=[_task("f1")],
            features=[Feature(name="feat", tasks=[_task("t1", ["f1"])])],
            integration=[],
        )
    ])
    valid, errors = validate_plan(plan)
    assert not valid
    assert any("cross-section" in e.lower() or "foundation" in e for e in errors)


def test_validate_plan_duplicate_ids():
    plan = Plan(waves=[
        Wave(
            name="W1",
            foundation=[_task("dup")],
            features=[Feature(name="feat", tasks=[_task("dup")])],
            integration=[],
        )
    ])
    valid, errors = validate_plan(plan)
    assert not valid
    assert any("Duplicate" in e for e in errors)


def test_validate_plan_file_overlap():
    plan = Plan(waves=[
        Wave(
            name="W1",
            foundation=[],
            features=[
                Feature(name="A", tasks=[_task("a1", files=["shared.py"])]),
                Feature(name="B", tasks=[_task("b1", files=["shared.py"])]),
            ],
            integration=[],
        )
    ])
    valid, errors = validate_plan(plan)
    assert not valid
    assert any("shared.py" in e for e in errors)


# ── map_concurrent ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_map_concurrent():
    results = await map_concurrent(
        [1, 2, 3],
        2,
        lambda x, i: asyncio.coroutine(lambda: x * 2)() if False else _async_double(x, i),
    )
    assert results == [2, 4, 6]


async def _async_double(x: int, _i: int) -> int:
    return x * 2


# ── execute_dag ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_dag_all_pass():
    tasks = [_task("a"), _task("b", ["a"])]

    async def run(task: Task) -> TaskResult:
        return TaskResult(
            id=task.id, title=task.title, agent="worker",
            exit_code=0, output="ok", stderr="", duration_ms=100,
        )

    results = await execute_dag(tasks, run, 2)
    assert len(results) == 2
    assert all(r.exit_code == 0 for r in results)


@pytest.mark.asyncio
async def test_execute_dag_skip_on_failure():
    tasks = [_task("a"), _task("b", ["a"])]

    async def run(task: Task) -> TaskResult:
        return TaskResult(
            id=task.id, title=task.title, agent="worker",
            exit_code=1 if task.id == "a" else 0,
            output="fail" if task.id == "a" else "ok",
            stderr="", duration_ms=100,
        )

    results = await execute_dag(tasks, run, 2)
    assert results[0].exit_code == 1
    assert results[1].exit_code == -1  # skipped
    assert "Skipped" in results[1].output
