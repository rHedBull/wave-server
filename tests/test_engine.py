"""Tests for the wave executor engine integration."""

import asyncio

import pytest

from wave_server.engine.runner import AgentRunner
from wave_server.engine.types import (
    Feature,
    Plan,
    RunnerConfig,
    RunnerResult,
    Task,
    Wave,
)
from wave_server.engine.wave_executor import WaveExecutorOptions, execute_wave
from wave_server.engine.state import (
    create_initial_state,
    mark_task_done,
    mark_task_failed,
    completed_task_ids,
    state_to_json,
    state_from_json,
)


class MockRunner:
    """Mock runner that returns configurable results."""

    def __init__(self, results: dict[str, int] | None = None):
        self.results = results or {}  # task_id -> exit_code
        self.spawned: list[str] = []

    async def spawn(self, config: RunnerConfig) -> RunnerResult:
        self.spawned.append(config.task_id)
        exit_code = self.results.get(config.task_id, 0)
        return RunnerResult(
            exit_code=exit_code,
            stdout=f'{{"type": "result", "result": "Task {config.task_id} done"}}',
            stderr="" if exit_code == 0 else "error occurred",
        )

    def extract_final_output(self, stdout: str) -> str:
        import json
        for line in stdout.split("\n"):
            try:
                msg = json.loads(line)
                if msg.get("type") == "result":
                    return msg.get("result", "")
            except (json.JSONDecodeError, KeyError):
                continue
        return stdout


def _task(id: str, agent: str = "worker", depends: list[str] | None = None) -> Task:
    return Task(id=id, title=f"Task {id}", agent=agent, depends=depends or [])


# ── Wave Executor ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_wave_foundation_only():
    wave = Wave(
        name="Test Wave",
        foundation=[_task("f1"), _task("f2", depends=["f1"])],
    )
    runner = MockRunner()
    opts = WaveExecutorOptions(wave=wave, wave_num=1, runner=runner)
    result = await execute_wave(opts)

    assert result.passed
    assert len(result.foundation_results) == 2
    assert all(r.exit_code == 0 for r in result.foundation_results)
    assert "f1" in runner.spawned
    assert "f2" in runner.spawned


@pytest.mark.asyncio
async def test_execute_wave_foundation_failure_skips_features():
    wave = Wave(
        name="Test Wave",
        foundation=[_task("f1")],
        features=[Feature(name="feat", tasks=[_task("t1")])],
    )
    runner = MockRunner(results={"f1": 1})
    opts = WaveExecutorOptions(wave=wave, wave_num=1, runner=runner)
    result = await execute_wave(opts)

    assert not result.passed
    assert len(result.feature_results) == 0  # skipped
    assert "t1" not in runner.spawned


@pytest.mark.asyncio
async def test_execute_wave_with_features():
    wave = Wave(
        name="Test Wave",
        features=[
            Feature(name="auth", tasks=[_task("a1"), _task("a2", depends=["a1"])]),
            Feature(name="profile", tasks=[_task("p1")]),
        ],
    )
    runner = MockRunner()
    opts = WaveExecutorOptions(wave=wave, wave_num=1, runner=runner)
    result = await execute_wave(opts)

    assert result.passed
    assert len(result.feature_results) == 2
    assert all(r.passed for r in result.feature_results)


@pytest.mark.asyncio
async def test_execute_wave_with_integration():
    wave = Wave(
        name="Test Wave",
        foundation=[_task("f1")],
        integration=[_task("i1")],
    )
    runner = MockRunner()
    opts = WaveExecutorOptions(wave=wave, wave_num=1, runner=runner)
    result = await execute_wave(opts)

    assert result.passed
    assert len(result.integration_results) == 1


@pytest.mark.asyncio
async def test_execute_wave_callbacks():
    started_tasks = []
    ended_tasks = []

    wave = Wave(name="Test", foundation=[_task("f1")])
    runner = MockRunner()
    opts = WaveExecutorOptions(
        wave=wave,
        wave_num=1,
        runner=runner,
        on_task_start=lambda phase, task: started_tasks.append(task.id),
        on_task_end=lambda phase, task, result: ended_tasks.append((task.id, result.exit_code)),
    )
    result = await execute_wave(opts)

    assert "f1" in started_tasks
    assert ("f1", 0) in ended_tasks


# ── State Management ───────────────────────────────────────────


def test_state_create_and_serialize():
    state = create_initial_state("plan.md")
    assert state.current_wave == 0
    assert state.task_states == {}

    json_str = state_to_json(state)
    restored = state_from_json(json_str)
    assert restored.plan_file == state.plan_file
    assert restored.current_wave == 0


def test_state_mark_tasks():
    state = create_initial_state("plan.md")
    mark_task_done(state, "t1")
    mark_task_failed(state, "t2")

    assert state.task_states["t1"] == "done"
    assert state.task_states["t2"] == "failed"

    ids = completed_task_ids(state)
    assert ids == {"t1"}


# ── MockRunner Protocol Compliance ─────────────────────────────


def test_mock_runner_is_agent_runner():
    from wave_server.engine.runner import AgentRunner
    runner = MockRunner()
    assert isinstance(runner, AgentRunner)
