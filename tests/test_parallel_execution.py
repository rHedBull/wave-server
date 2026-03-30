"""Complex tests for parallel execution and wave transition edge cases.

Targets the bugs we found in production:
1. SQLite "database is locked" from concurrent DB writes (db_lock fix)
2. Runner CLI flag issues (--print --verbose --dangerously-skip-permissions)
3. Stuck executions on wave transitions
4. Event ordering under concurrency
5. Task counting accuracy with parallel completions
6. Phase transitions with mixed success/failure across features
7. Stress tests with high concurrency and many tasks

Tests use the real execution manager (_run_execution) with mocked runner
and in-memory SQLite to exercise the full concurrent code paths.
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from wave_server.db import Base
from wave_server.engine.types import (
    Feature,
    RunnerConfig,
    RunnerResult,
    Task,
    Wave,
)
from wave_server.engine.wave_executor import WaveExecutorOptions, execute_wave
from wave_server.models import Event, Execution, Project, ProjectRepository, Sequence


# ── Helpers ────────────────────────────────────────────────────


def _task(
    id: str,
    agent: str = "worker",
    depends: list[str] | None = None,
    files: list[str] | None = None,
    description: str = "",
) -> Task:
    return Task(
        id=id,
        title=f"Task {id}",
        agent=agent,
        depends=depends or [],
        files=files or [f"{id}.py"],
        description=description or f"Do task {id}",
    )


class MockRunner:
    """Mock runner with configurable per-task results, delays, and tracking."""

    def __init__(
        self,
        results: dict[str, int] | None = None,
        delays: dict[str, float] | None = None,
        default_delay: float = 0,
    ):
        self.results = results or {}
        self.delays = delays or {}
        self.default_delay = default_delay
        self.spawned: list[str] = []
        self.spawn_times: dict[str, float] = {}
        self.completion_times: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def spawn(self, config: RunnerConfig) -> RunnerResult:
        async with self._lock:
            self.spawned.append(config.task_id)
            self.spawn_times[config.task_id] = time.monotonic()

        delay = self.delays.get(config.task_id, self.default_delay)
        if delay:
            await asyncio.sleep(delay)

        async with self._lock:
            self.completion_times[config.task_id] = time.monotonic()

        exit_code = self.results.get(config.task_id, 0)
        return RunnerResult(
            exit_code=exit_code,
            stdout=json.dumps(
                {"type": "result", "result": f"Output for {config.task_id}"}
            ),
            stderr="" if exit_code == 0 else f"Error in {config.task_id}",
        )

    def extract_final_output(self, stdout: str) -> str:
        for line in stdout.split("\n"):
            try:
                msg = json.loads(line)
                if msg.get("type") == "result":
                    return msg.get("result", "")
            except (json.JSONDecodeError, KeyError):
                continue
        return stdout


# ── Fixtures for full execution manager tests ─────────────────


@pytest_asyncio.fixture
async def test_db():
    """In-memory SQLite with async_session patched."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    with patch("wave_server.engine.execution_manager.async_session", session_factory):
        yield session_factory
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture(autouse=True)
def _mock_storage():
    with patch("wave_server.engine.execution_manager.storage") as mock_storage:
        mock_storage.read_spec.return_value = "# Test Spec"
        mock_storage.read_plan.return_value = None  # Overridden per test
        mock_storage.write_output.return_value = None
        mock_storage.write_transcript.return_value = None
        mock_storage.write_log.return_value = None
        mock_storage.write_task_log.return_value = None
        mock_storage.append_log.return_value = None
        yield mock_storage


@pytest.fixture(autouse=True)
def _mock_git():
    with patch(
        "wave_server.engine.execution_manager._get_git_sha",
        return_value="deadbeef",
    ):
        yield


async def _setup(
    session_factory,
    plan_content: str,
    repo_path: str,
    concurrency: int = 4,
) -> tuple[str, str]:
    """Create project + repo + sequence + execution. Returns (seq_id, exec_id)."""
    async with session_factory() as db:
        project = Project(name="test-project")
        db.add(project)
        await db.commit()
        await db.refresh(project)

        repo = ProjectRepository(project_id=project.id, path=repo_path, label="test")
        db.add(repo)
        await db.commit()

        sequence = Sequence(project_id=project.id, name="test-seq")
        db.add(sequence)
        await db.commit()
        await db.refresh(sequence)

        execution = Execution(
            sequence_id=sequence.id,
            runtime="claude",
            config=json.dumps({"concurrency": concurrency, "timeout_ms": 60000}),
        )
        db.add(execution)
        await db.commit()
        await db.refresh(execution)

        return sequence.id, execution.id


async def _get_execution(sf, exec_id: str) -> Execution:
    async with sf() as db:
        return await db.get(Execution, exec_id)


async def _get_events(sf, exec_id: str) -> list[Event]:
    async with sf() as db:
        result = await db.execute(
            select(Event)
            .where(Event.execution_id == exec_id)
            .order_by(Event.created_at)
        )
        return list(result.scalars().all())


# ═══════════════════════════════════════════════════════════════
# PARALLEL FOUNDATION TASKS — the core concurrency bug area
# ═══════════════════════════════════════════════════════════════


class TestParallelFoundationTasks:
    """Tests for concurrent tasks within a foundation section.

    This is where the "database is locked" bug manifested: multiple
    independent foundation tasks completing nearly simultaneously
    all try to commit to the DB through callbacks.
    """

    PARALLEL_FOUNDATION_PLAN = """\
# Implementation Plan
<!-- format: v2 -->
## Project Structure
```
src/
```

## Data Schemas
No schemas.

<!-- format: v2 -->
## Project Structure
```
src/
```

## Data Schemas
No schemas.


## Goal
Test parallel foundation

## Wave 1: Setup

### Foundation
#### Task f1: Setup database
- **Files**: `db.py`
- **Depends**: (none)
- **Description**: Setup database

#### Task f2: Setup config
- **Files**: `config.py`
- **Depends**: (none)
- **Description**: Setup config

#### Task f3: Setup logging
- **Files**: `logging.py`
- **Depends**: (none)
- **Description**: Setup logging

#### Task f4: Setup auth
- **Files**: `auth.py`
- **Depends**: (none)
- **Description**: Setup auth
"""

    @pytest.mark.asyncio
    async def test_four_parallel_foundation_all_complete(
        self, test_db, _mock_storage, tmp_path
    ):
        """4 independent foundation tasks should all complete without DB errors."""
        _mock_storage.read_plan.return_value = self.PARALLEL_FOUNDATION_PLAN
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()

        runner = MockRunner(default_delay=0.01)
        seq_id, exec_id = await _setup(
            test_db, self.PARALLEL_FOUNDATION_PLAN, str(repo_dir)
        )

        with patch(
            "wave_server.engine.execution_manager.get_runner", return_value=runner
        ):
            from wave_server.engine.execution_manager import _run_execution

            await _run_execution(exec_id, seq_id)

        exc = await _get_execution(test_db, exec_id)
        assert exc.status == "completed", f"Expected completed, got {exc.status}"
        assert exc.completed_tasks == 4

    @pytest.mark.asyncio
    async def test_parallel_tasks_no_lost_events(
        self, test_db, _mock_storage, tmp_path
    ):
        """All task_started and task_completed events must be recorded."""
        _mock_storage.read_plan.return_value = self.PARALLEL_FOUNDATION_PLAN
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()

        runner = MockRunner(default_delay=0.01)
        seq_id, exec_id = await _setup(
            test_db, self.PARALLEL_FOUNDATION_PLAN, str(repo_dir)
        )

        with patch(
            "wave_server.engine.execution_manager.get_runner", return_value=runner
        ):
            from wave_server.engine.execution_manager import _run_execution

            await _run_execution(exec_id, seq_id)

        events = await _get_events(test_db, exec_id)
        event_types = [e.event_type for e in events]

        assert event_types.count("task_started") == 4
        assert event_types.count("task_completed") == 4

    @pytest.mark.asyncio
    async def test_simultaneous_completions_no_db_error(
        self, test_db, _mock_storage, tmp_path
    ):
        """Tasks completing at exactly the same time should not cause DB contention."""
        _mock_storage.read_plan.return_value = self.PARALLEL_FOUNDATION_PLAN
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()

        # All tasks complete instantly — maximum contention
        runner = MockRunner(default_delay=0)
        seq_id, exec_id = await _setup(
            test_db, self.PARALLEL_FOUNDATION_PLAN, str(repo_dir), concurrency=4
        )

        with patch(
            "wave_server.engine.execution_manager.get_runner", return_value=runner
        ):
            from wave_server.engine.execution_manager import _run_execution

            await _run_execution(exec_id, seq_id)

        exc = await _get_execution(test_db, exec_id)
        assert exc.status == "completed"
        assert exc.completed_tasks == 4

    @pytest.mark.asyncio
    async def test_staggered_completions(self, test_db, _mock_storage, tmp_path):
        """Tasks completing at staggered times should all be recorded correctly."""
        _mock_storage.read_plan.return_value = self.PARALLEL_FOUNDATION_PLAN
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()

        runner = MockRunner(delays={"f1": 0.01, "f2": 0.02, "f3": 0.03, "f4": 0.04})
        seq_id, exec_id = await _setup(
            test_db, self.PARALLEL_FOUNDATION_PLAN, str(repo_dir)
        )

        with patch(
            "wave_server.engine.execution_manager.get_runner", return_value=runner
        ):
            from wave_server.engine.execution_manager import _run_execution

            await _run_execution(exec_id, seq_id)

        exc = await _get_execution(test_db, exec_id)
        assert exc.status == "completed"
        assert exc.completed_tasks == 4

        events = await _get_events(test_db, exec_id)
        completed_ids = [
            json.loads(e.payload)["task_id"]
            for e in events
            if e.event_type == "task_completed"
        ]
        assert set(completed_ids) == {"f1", "f2", "f3", "f4"}

    @pytest.mark.asyncio
    async def test_one_parallel_task_fails_others_still_complete(
        self, test_db, _mock_storage, tmp_path
    ):
        """When one parallel foundation task fails, others may still run (DAG independent)."""
        _mock_storage.read_plan.return_value = self.PARALLEL_FOUNDATION_PLAN
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()

        runner = MockRunner(results={"f2": 1}, default_delay=0.01)
        seq_id, exec_id = await _setup(
            test_db, self.PARALLEL_FOUNDATION_PLAN, str(repo_dir)
        )

        with patch(
            "wave_server.engine.execution_manager.get_runner", return_value=runner
        ):
            from wave_server.engine.execution_manager import _run_execution

            await _run_execution(exec_id, seq_id)

        exc = await _get_execution(test_db, exec_id)
        assert exc.status == "failed"

        events = await _get_events(test_db, exec_id)
        failed_events = [e for e in events if e.event_type == "task_failed"]
        assert len(failed_events) >= 1
        assert any(json.loads(e.payload)["task_id"] == "f2" for e in failed_events)


# ═══════════════════════════════════════════════════════════════
# WAVE TRANSITIONS — moving between waves
# ═══════════════════════════════════════════════════════════════


class TestWaveTransitions:
    """Tests for correct state transitions between waves.

    The "stuck execution" bug happened when wave 1 completed but
    wave 2 never started due to an unhandled exception during
    the transition (DB commit between waves).
    """

    THREE_WAVE_PLAN = """\
# Implementation Plan
<!-- format: v2 -->
## Project Structure
```
src/
```

## Data Schemas
No schemas.

<!-- format: v2 -->
## Project Structure
```
src/
```

## Data Schemas
No schemas.


## Goal
Test wave transitions

## Wave 1: Foundation


### Foundation

#### Task 1-1: Setup
- **Files**: `setup.py`
- **Depends**: (none)
- **Description**: Initial setup

## Wave 2: Core


### Foundation

#### Task 2-1: Core A
- **Files**: `core_a.py`
- **Depends**: (none)
- **Description**: Core feature A

#### Task 2-2: Core B
- **Files**: `core_b.py`
- **Depends**: (none)
- **Description**: Core feature B

## Wave 3: Finish


### Foundation

#### Task 3-1: Polish
- **Files**: `polish.py`
- **Depends**: (none)
- **Description**: Polish everything
"""

    @pytest.mark.asyncio
    async def test_three_wave_all_pass(self, test_db, _mock_storage, tmp_path):
        """All 3 waves should execute sequentially."""
        _mock_storage.read_plan.return_value = self.THREE_WAVE_PLAN
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()

        runner = MockRunner()
        seq_id, exec_id = await _setup(test_db, self.THREE_WAVE_PLAN, str(repo_dir))

        with patch(
            "wave_server.engine.execution_manager.get_runner", return_value=runner
        ):
            from wave_server.engine.execution_manager import _run_execution

            await _run_execution(exec_id, seq_id)

        exc = await _get_execution(test_db, exec_id)
        assert exc.status == "completed"
        assert exc.completed_tasks == 4
        assert set(runner.spawned) == {"1-1", "2-1", "2-2", "3-1"}

    @pytest.mark.asyncio
    async def test_wave_transition_emits_phase_changed_events(
        self, test_db, _mock_storage, tmp_path
    ):
        """Each wave should emit a phase_changed event."""
        _mock_storage.read_plan.return_value = self.THREE_WAVE_PLAN
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()

        runner = MockRunner()
        seq_id, exec_id = await _setup(test_db, self.THREE_WAVE_PLAN, str(repo_dir))

        with patch(
            "wave_server.engine.execution_manager.get_runner", return_value=runner
        ):
            from wave_server.engine.execution_manager import _run_execution

            await _run_execution(exec_id, seq_id)

        events = await _get_events(test_db, exec_id)
        phase_events = [e for e in events if e.event_type == "phase_changed"]
        assert len(phase_events) == 3

        wave_names = [json.loads(e.payload)["wave_name"] for e in phase_events]
        assert wave_names == ["Foundation", "Core", "Finish"]

    @pytest.mark.asyncio
    async def test_wave2_failure_stops_at_wave2(self, test_db, _mock_storage, tmp_path):
        """If wave 2 fails, wave 3 should not execute."""
        _mock_storage.read_plan.return_value = self.THREE_WAVE_PLAN
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()

        runner = MockRunner(results={"2-1": 1})
        seq_id, exec_id = await _setup(test_db, self.THREE_WAVE_PLAN, str(repo_dir))

        with patch(
            "wave_server.engine.execution_manager.get_runner", return_value=runner
        ):
            from wave_server.engine.execution_manager import _run_execution

            await _run_execution(exec_id, seq_id)

        exc = await _get_execution(test_db, exec_id)
        assert exc.status == "failed"
        assert "3-1" not in runner.spawned

        events = await _get_events(test_db, exec_id)
        wave_completed = [e for e in events if e.event_type == "wave_completed"]
        # Wave 1 passes, Wave 2 fails, no Wave 3 completion
        assert len(wave_completed) == 2
        payloads = [json.loads(e.payload) for e in wave_completed]
        assert payloads[0]["passed"] is True
        assert payloads[1]["passed"] is False

    @pytest.mark.asyncio
    async def test_current_wave_updates_during_execution(
        self, test_db, _mock_storage, tmp_path
    ):
        """current_wave should be updated as each wave starts."""
        _mock_storage.read_plan.return_value = self.THREE_WAVE_PLAN
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()

        wave_indices_seen = []
        original_runner = MockRunner()

        class TrackingRunner:
            async def spawn(self, config: RunnerConfig) -> RunnerResult:
                # Read current_wave from DB mid-execution
                async with test_db() as db:
                    exc = await db.get(Execution, exec_id)
                    wave_indices_seen.append((config.task_id, exc.current_wave))
                return await original_runner.spawn(config)

            def extract_final_output(self, stdout: str) -> str:
                return original_runner.extract_final_output(stdout)

        seq_id, exec_id = await _setup(test_db, self.THREE_WAVE_PLAN, str(repo_dir))

        with patch(
            "wave_server.engine.execution_manager.get_runner",
            return_value=TrackingRunner(),
        ):
            from wave_server.engine.execution_manager import _run_execution

            await _run_execution(exec_id, seq_id)

        # Each task should see its own wave index
        for task_id, wave_idx in wave_indices_seen:
            if task_id.startswith("1-"):
                assert wave_idx == 0
            elif task_id.startswith("2-"):
                assert wave_idx == 1
            elif task_id.startswith("3-"):
                assert wave_idx == 2

    @pytest.mark.asyncio
    async def test_wave_transition_with_parallel_tasks_in_wave2(
        self, test_db, _mock_storage, tmp_path
    ):
        """Wave 1 (serial) → Wave 2 (parallel) transition should work cleanly."""
        plan = """\
# Implementation Plan
<!-- format: v2 -->
## Project Structure
```
src/
```

## Data Schemas
No schemas.

<!-- format: v2 -->
## Project Structure
```
src/
```

## Data Schemas
No schemas.


## Goal
Serial to parallel wave transition

## Wave 1: Setup


### Foundation

#### Task 1-1: Init
- **Files**: `init.py`
- **Depends**: (none)
- **Description**: Init

## Wave 2: Build


### Foundation

#### Task 2-1: Build A
- **Files**: `a.py`
- **Depends**: (none)
- **Description**: Build A

#### Task 2-2: Build B
- **Files**: `b.py`
- **Depends**: (none)
- **Description**: Build B

#### Task 2-3: Build C
- **Files**: `c.py`
- **Depends**: (none)
- **Description**: Build C
"""
        _mock_storage.read_plan.return_value = plan
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()

        runner = MockRunner(default_delay=0.01)
        seq_id, exec_id = await _setup(test_db, plan, str(repo_dir), concurrency=3)

        with patch(
            "wave_server.engine.execution_manager.get_runner", return_value=runner
        ):
            from wave_server.engine.execution_manager import _run_execution

            await _run_execution(exec_id, seq_id)

        exc = await _get_execution(test_db, exec_id)
        assert exc.status == "completed"
        assert exc.completed_tasks == 4
        assert set(runner.spawned) == {"1-1", "2-1", "2-2", "2-3"}


# ═══════════════════════════════════════════════════════════════
# PARALLEL FEATURES — multiple independent feature groups
# ═══════════════════════════════════════════════════════════════


class TestParallelFeatures:
    """Tests for running multiple features concurrently within a wave."""

    MULTI_FEATURE_PLAN = """\
# Implementation Plan
<!-- format: v2 -->
## Project Structure
```
src/
```

## Data Schemas
No schemas.

<!-- format: v2 -->
## Project Structure
```
src/
```

## Data Schemas
No schemas.


## Goal
Test parallel features

## Wave 1: Implementation

### Foundation
#### Task f1: Shared setup
- **Agent**: worker
- **Files**: `shared.py`
- **Depends**: (none)
- **Description**: Shared setup

### Feature: Auth
Files: `auth.py`

#### Task a1: Login
- **Agent**: worker
- **Files**: `auth.py`
- **Depends**: (none)
- **Description**: Login endpoint

#### Task a2: Register
- **Agent**: worker
- **Files**: `auth_reg.py`
- **Depends**: a1
- **Description**: Registration

### Feature: Billing
Files: `billing.py`

#### Task b1: Invoices
- **Agent**: worker
- **Files**: `billing.py`
- **Depends**: (none)
- **Description**: Invoicing

### Integration
#### Task i1: Verify all
- **Agent**: wave-verifier
- **Files**: `auth.py`, `billing.py`
- **Depends**: (none)
- **Description**: Verify integration
"""

    @pytest.mark.asyncio
    async def test_foundation_then_features_then_integration(self):
        """Foundation → Features (parallel) → Integration ordering."""
        wave = Wave(
            name="W1",
            foundation=[_task("f1")],
            features=[
                Feature(name="auth", tasks=[_task("a1"), _task("a2", depends=["a1"])]),
                Feature(name="billing", tasks=[_task("b1")]),
            ],
            integration=[_task("i1")],
        )
        runner = MockRunner(default_delay=0.01)
        result = await execute_wave(
            WaveExecutorOptions(wave=wave, wave_num=1, runner=runner)
        )
        assert result.passed

        # Foundation must run before features
        assert runner.spawn_times["f1"] < runner.spawn_times["a1"]
        assert runner.spawn_times["f1"] < runner.spawn_times["b1"]

        # Integration must run after all features
        assert runner.completion_times["a2"] < runner.spawn_times["i1"]
        assert runner.completion_times["b1"] < runner.spawn_times["i1"]

    @pytest.mark.asyncio
    async def test_features_run_in_parallel(self):
        """Independent features should start concurrently."""
        wave = Wave(
            name="W1",
            features=[
                Feature(name="auth", tasks=[_task("a1")]),
                Feature(name="billing", tasks=[_task("b1")]),
                Feature(name="profile", tasks=[_task("p1")]),
            ],
        )
        runner = MockRunner(default_delay=0.05)
        result = await execute_wave(
            WaveExecutorOptions(wave=wave, wave_num=1, runner=runner, max_concurrency=4)
        )
        assert result.passed
        assert set(runner.spawned) == {"a1", "b1", "p1"}

    @pytest.mark.asyncio
    async def test_one_feature_fails_other_still_completes(self):
        """With parallel features, one failure shouldn't prevent others from completing."""
        wave = Wave(
            name="W1",
            features=[
                Feature(name="auth", tasks=[_task("a1")]),
                Feature(name="billing", tasks=[_task("b1")]),
            ],
            integration=[_task("i1")],
        )
        runner = MockRunner(results={"b1": 1}, default_delay=0.01)
        result = await execute_wave(
            WaveExecutorOptions(wave=wave, wave_num=1, runner=runner)
        )
        assert not result.passed

        # Auth should have passed
        auth = next(r for r in result.feature_results if r.name == "auth")
        billing = next(r for r in result.feature_results if r.name == "billing")
        assert auth.passed
        assert not billing.passed

        # Integration should be skipped
        assert len(result.integration_results) == 0
        assert "i1" not in runner.spawned

    @pytest.mark.asyncio
    async def test_sequential_tasks_within_parallel_features(self):
        """Tasks within each feature should respect their internal ordering."""
        wave = Wave(
            name="W1",
            features=[
                Feature(
                    name="auth",
                    tasks=[
                        _task("a1"),
                        _task("a2", depends=["a1"]),
                        _task("a3", depends=["a2"]),
                    ],
                ),
                Feature(
                    name="billing",
                    tasks=[
                        _task("b1"),
                        _task("b2", depends=["b1"]),
                    ],
                ),
            ],
        )
        runner = MockRunner(default_delay=0.01)
        result = await execute_wave(
            WaveExecutorOptions(wave=wave, wave_num=1, runner=runner)
        )
        assert result.passed

        # Verify ordering within each feature
        assert runner.spawn_times["a1"] < runner.spawn_times["a2"]
        assert runner.spawn_times["a2"] < runner.spawn_times["a3"]
        assert runner.spawn_times["b1"] < runner.spawn_times["b2"]

    @pytest.mark.asyncio
    async def test_feature_with_internal_dag(self):
        """Feature with a diamond dependency pattern."""
        wave = Wave(
            name="W1",
            features=[
                Feature(
                    name="complex",
                    tasks=[
                        _task("root"),
                        _task("left", depends=["root"]),
                        _task("right", depends=["root"]),
                        _task("merge", depends=["left", "right"]),
                    ],
                ),
            ],
        )
        runner = MockRunner(default_delay=0.01)
        result = await execute_wave(
            WaveExecutorOptions(wave=wave, wave_num=1, runner=runner)
        )
        assert result.passed

        # Root must run before left and right
        assert runner.spawn_times["root"] < runner.spawn_times["left"]
        assert runner.spawn_times["root"] < runner.spawn_times["right"]
        # Merge must run after both left and right
        assert runner.completion_times["left"] < runner.spawn_times["merge"]
        assert runner.completion_times["right"] < runner.spawn_times["merge"]


# ═══════════════════════════════════════════════════════════════
# EVENT ORDERING AND CONSISTENCY
# ═══════════════════════════════════════════════════════════════


class TestEventOrderingUnderConcurrency:
    """Verify events maintain logical ordering even with concurrent tasks."""

    @pytest.mark.asyncio
    async def test_run_started_is_first_run_completed_is_last(
        self, test_db, _mock_storage, tmp_path
    ):
        """run_started must be first, run_completed must be last."""
        plan = """\
# Implementation Plan
<!-- format: v2 -->
## Project Structure
```
src/
```

## Data Schemas
No schemas.

<!-- format: v2 -->
## Project Structure
```
src/
```

## Data Schemas
No schemas.


## Goal
Event ordering test

## Wave 1: Work


### Foundation

#### Task 1-1: A
- **Files**: `a.py`
- **Depends**: (none)
- **Description**: A

#### Task 1-2: B
- **Files**: `b.py`
- **Depends**: (none)
- **Description**: B
"""
        _mock_storage.read_plan.return_value = plan
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()

        runner = MockRunner(default_delay=0.01)
        seq_id, exec_id = await _setup(test_db, plan, str(repo_dir))

        with patch(
            "wave_server.engine.execution_manager.get_runner", return_value=runner
        ):
            from wave_server.engine.execution_manager import _run_execution

            await _run_execution(exec_id, seq_id)

        events = await _get_events(test_db, exec_id)
        types = [e.event_type for e in events]

        assert types[0] == "run_started"
        assert types[-1] == "run_completed"

    @pytest.mark.asyncio
    async def test_task_started_before_task_completed(
        self, test_db, _mock_storage, tmp_path
    ):
        """For each task, started must come before completed."""
        plan = """\
# Implementation Plan
<!-- format: v2 -->
## Project Structure
```
src/
```

## Data Schemas
No schemas.

<!-- format: v2 -->
## Project Structure
```
src/
```

## Data Schemas
No schemas.


## Goal
Ordering

## Wave 1: Work


### Foundation

#### Task 1-1: A
- **Files**: `a.py`
- **Depends**: (none)
- **Description**: A

#### Task 1-2: B
- **Files**: `b.py`
- **Depends**: (none)
- **Description**: B

#### Task 1-3: C
- **Files**: `c.py`
- **Depends**: (none)
- **Description**: C
"""
        _mock_storage.read_plan.return_value = plan
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()

        runner = MockRunner(default_delay=0.01)
        seq_id, exec_id = await _setup(test_db, plan, str(repo_dir))

        with patch(
            "wave_server.engine.execution_manager.get_runner", return_value=runner
        ):
            from wave_server.engine.execution_manager import _run_execution

            await _run_execution(exec_id, seq_id)

        events = await _get_events(test_db, exec_id)
        for task_id in ["1-1", "1-2", "1-3"]:
            started_idx = next(
                i
                for i, e in enumerate(events)
                if e.event_type == "task_started" and e.task_id == task_id
            )
            completed_idx = next(
                i
                for i, e in enumerate(events)
                if e.event_type == "task_completed" and e.task_id == task_id
            )
            assert started_idx < completed_idx, (
                f"task_started for {task_id} (idx={started_idx}) should come before "
                f"task_completed (idx={completed_idx})"
            )

    @pytest.mark.asyncio
    async def test_phase_changed_before_wave_tasks(
        self, test_db, _mock_storage, tmp_path
    ):
        """phase_changed for a wave should come before that wave's tasks."""
        plan = """\
# Implementation Plan
<!-- format: v2 -->
## Project Structure
```
src/
```

## Data Schemas
No schemas.

<!-- format: v2 -->
## Project Structure
```
src/
```

## Data Schemas
No schemas.


## Goal
Phase ordering

## Wave 1: First


### Foundation

#### Task 1-1: A
- **Files**: `a.py`
- **Depends**: (none)
- **Description**: A

## Wave 2: Second


### Foundation

#### Task 2-1: B
- **Files**: `b.py`
- **Depends**: (none)
- **Description**: B
"""
        _mock_storage.read_plan.return_value = plan
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()

        runner = MockRunner()
        seq_id, exec_id = await _setup(test_db, plan, str(repo_dir))

        with patch(
            "wave_server.engine.execution_manager.get_runner", return_value=runner
        ):
            from wave_server.engine.execution_manager import _run_execution

            await _run_execution(exec_id, seq_id)

        events = await _get_events(test_db, exec_id)

        # Find phase_changed for wave 2
        phase2_idx = next(
            i
            for i, e in enumerate(events)
            if e.event_type == "phase_changed"
            and json.loads(e.payload).get("wave_name") == "Second"
        )
        # Find task_started for task 2-1
        task2_started_idx = next(
            i
            for i, e in enumerate(events)
            if e.event_type == "task_started" and e.task_id == "2-1"
        )
        assert phase2_idx < task2_started_idx

    @pytest.mark.asyncio
    async def test_wave_completed_after_all_wave_tasks(
        self, test_db, _mock_storage, tmp_path
    ):
        """wave_completed should come after all task events for that wave."""
        plan = """\
# Implementation Plan
<!-- format: v2 -->
## Project Structure
```
src/
```

## Data Schemas
No schemas.

<!-- format: v2 -->
## Project Structure
```
src/
```

## Data Schemas
No schemas.


## Goal
Wave completion ordering

## Wave 1: Work


### Foundation

#### Task 1-1: A
- **Files**: `a.py`
- **Depends**: (none)
- **Description**: A

#### Task 1-2: B
- **Files**: `b.py`
- **Depends**: (none)
- **Description**: B
"""
        _mock_storage.read_plan.return_value = plan
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()

        runner = MockRunner(default_delay=0.01)
        seq_id, exec_id = await _setup(test_db, plan, str(repo_dir))

        with patch(
            "wave_server.engine.execution_manager.get_runner", return_value=runner
        ):
            from wave_server.engine.execution_manager import _run_execution

            await _run_execution(exec_id, seq_id)

        events = await _get_events(test_db, exec_id)

        wave_completed_idx = next(
            i for i, e in enumerate(events) if e.event_type == "wave_completed"
        )
        last_task_idx = max(
            i
            for i, e in enumerate(events)
            if e.event_type in ("task_completed", "task_failed")
            and e.task_id in ("1-1", "1-2")
        )
        assert wave_completed_idx > last_task_idx


# ═══════════════════════════════════════════════════════════════
# TASK COUNT ACCURACY
# ═══════════════════════════════════════════════════════════════


class TestTaskCountAccuracy:
    """Verify completed_tasks counter is accurate with concurrent updates."""

    @pytest.mark.asyncio
    async def test_completed_count_exact_with_parallel(
        self, test_db, _mock_storage, tmp_path
    ):
        """completed_tasks should equal actual completions, no off-by-one errors."""
        plan = """\
# Implementation Plan
<!-- format: v2 -->
## Project Structure
```
src/
```

## Data Schemas
No schemas.

<!-- format: v2 -->
## Project Structure
```
src/
```

## Data Schemas
No schemas.


## Goal
Count test

## Wave 1: Work


### Foundation

#### Task 1-1: A
- **Files**: `a.py`
- **Depends**: (none)
- **Description**: A

#### Task 1-2: B
- **Files**: `b.py`
- **Depends**: (none)
- **Description**: B

#### Task 1-3: C
- **Files**: `c.py`
- **Depends**: (none)
- **Description**: C

#### Task 1-4: D
- **Files**: `d.py`
- **Depends**: (none)
- **Description**: D

#### Task 1-5: E
- **Files**: `e.py`
- **Depends**: (none)
- **Description**: E
"""
        _mock_storage.read_plan.return_value = plan
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()

        runner = MockRunner(default_delay=0.01)
        seq_id, exec_id = await _setup(test_db, plan, str(repo_dir), concurrency=5)

        with patch(
            "wave_server.engine.execution_manager.get_runner", return_value=runner
        ):
            from wave_server.engine.execution_manager import _run_execution

            await _run_execution(exec_id, seq_id)

        exc = await _get_execution(test_db, exec_id)
        assert exc.completed_tasks == 5
        assert exc.total_tasks == 5

    @pytest.mark.asyncio
    async def test_completed_count_with_mixed_pass_fail(
        self, test_db, _mock_storage, tmp_path
    ):
        """Failed tasks still increment the completed counter.

        Flat tasks go into a single 'default' feature and execute
        sequentially — when 1-2 fails, 1-3 is skipped (never runs).
        Use foundation tasks for truly parallel execution.
        """
        plan = """\
# Implementation Plan
<!-- format: v2 -->
## Project Structure
```
src/
```

## Data Schemas
No schemas.

<!-- format: v2 -->
## Project Structure
```
src/
```

## Data Schemas
No schemas.


## Goal
Mixed count

## Wave 1: Work

### Foundation
#### Task 1-1: A
- **Files**: `a.py`
- **Depends**: (none)
- **Description**: A

#### Task 1-2: B
- **Files**: `b.py`
- **Depends**: (none)
- **Description**: B

#### Task 1-3: C
- **Files**: `c.py`
- **Depends**: (none)
- **Description**: C
"""
        _mock_storage.read_plan.return_value = plan
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()

        # 1-2 fails; 1-1 and 1-3 pass. All are independent foundation tasks
        # so all 3 run (parallel DAG), and all 3 count as completed.
        runner = MockRunner(results={"1-2": 1}, default_delay=0.01)
        seq_id, exec_id = await _setup(test_db, plan, str(repo_dir))

        with patch(
            "wave_server.engine.execution_manager.get_runner", return_value=runner
        ):
            from wave_server.engine.execution_manager import _run_execution

            await _run_execution(exec_id, seq_id)

        exc = await _get_execution(test_db, exec_id)
        # All 3 tasks ran (independent in DAG), 1 failed
        assert exc.completed_tasks == 3
        assert exc.status == "failed"

    @pytest.mark.asyncio
    async def test_total_tasks_spans_multiple_waves(
        self, test_db, _mock_storage, tmp_path
    ):
        """total_tasks should count tasks across all waves."""
        plan = """\
# Implementation Plan
<!-- format: v2 -->
## Project Structure
```
src/
```

## Data Schemas
No schemas.

<!-- format: v2 -->
## Project Structure
```
src/
```

## Data Schemas
No schemas.


## Goal
Multi-wave count

## Wave 1: A


### Foundation

#### Task 1-1: A
- **Files**: `a.py`
- **Depends**: (none)
- **Description**: A

## Wave 2: B


### Foundation

#### Task 2-1: B
- **Files**: `b.py`
- **Depends**: (none)
- **Description**: B

#### Task 2-2: C
- **Files**: `c.py`
- **Depends**: (none)
- **Description**: C

## Wave 3: D


### Foundation

#### Task 3-1: D
- **Files**: `d.py`
- **Depends**: (none)
- **Description**: D
"""
        _mock_storage.read_plan.return_value = plan
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()

        runner = MockRunner()
        seq_id, exec_id = await _setup(test_db, plan, str(repo_dir))

        with patch(
            "wave_server.engine.execution_manager.get_runner", return_value=runner
        ):
            from wave_server.engine.execution_manager import _run_execution

            await _run_execution(exec_id, seq_id)

        exc = await _get_execution(test_db, exec_id)
        assert exc.total_tasks == 4
        assert exc.completed_tasks == 4


# ═══════════════════════════════════════════════════════════════
# COMPLEX MULTI-WAVE + MULTI-FEATURE SCENARIOS
# ═══════════════════════════════════════════════════════════════


class TestComplexScenarios:
    """End-to-end tests with complex plans combining multiple features,
    foundations, integrations, and wave transitions."""

    @pytest.mark.asyncio
    async def test_full_lifecycle_foundation_features_integration(self):
        """Complete wave with all three phases."""
        wave = Wave(
            name="Complete",
            foundation=[_task("f1"), _task("f2")],
            features=[
                Feature(name="auth", tasks=[_task("a1"), _task("a2", depends=["a1"])]),
                Feature(name="api", tasks=[_task("p1")]),
            ],
            integration=[_task("i1"), _task("i2", depends=["i1"])],
        )
        started_phases = []
        ended_phases = []
        runner = MockRunner(default_delay=0.01)

        result = await execute_wave(
            WaveExecutorOptions(
                wave=wave,
                wave_num=1,
                runner=runner,
                on_task_start=lambda phase, task: started_phases.append(
                    (phase, task.id)
                ),
                on_task_end=lambda phase, task, result: ended_phases.append(
                    (phase, task.id)
                ),
            )
        )
        assert result.passed

        # Verify phase ordering
        foundation_tasks = [t for p, t in started_phases if p == "foundation"]
        feature_tasks = [t for p, t in started_phases if p.startswith("feature:")]
        integration_tasks = [t for p, t in started_phases if p == "integration"]

        assert set(foundation_tasks) == {"f1", "f2"}
        assert set(feature_tasks) == {"a1", "a2", "p1"}
        assert integration_tasks == ["i1", "i2"]

    @pytest.mark.asyncio
    async def test_multi_wave_with_foundation_and_features(
        self, test_db, _mock_storage, tmp_path
    ):
        """Two waves each with foundation + features."""
        plan = """\
# Implementation Plan
<!-- format: v2 -->
## Project Structure
```
src/
```

## Data Schemas
No schemas.

<!-- format: v2 -->
## Project Structure
```
src/
```

## Data Schemas
No schemas.


## Goal
Complex multi-wave

## Wave 1: Setup

### Foundation
#### Task f1: Database
- **Agent**: worker
- **Files**: `db.py`
- **Depends**: (none)
- **Description**: Setup DB

### Feature: Auth
Files: `auth.py`

#### Task a1: Login
- **Agent**: worker
- **Files**: `auth.py`
- **Depends**: (none)
- **Description**: Login

### Feature: Profile
Files: `profile.py`

#### Task p1: Profile page
- **Agent**: worker
- **Files**: `profile.py`
- **Depends**: (none)
- **Description**: Profile

## Wave 2: Tests

### Foundation
#### Task t1: Test auth
- **Agent**: worker
- **Files**: `test_auth.py`
- **Depends**: (none)
- **Description**: Test auth

#### Task t2: Test profile
- **Agent**: worker
- **Files**: `test_profile.py`
- **Depends**: (none)
- **Description**: Test profile
"""
        _mock_storage.read_plan.return_value = plan
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()

        runner = MockRunner(default_delay=0.01)
        seq_id, exec_id = await _setup(test_db, plan, str(repo_dir))

        with patch(
            "wave_server.engine.execution_manager.get_runner", return_value=runner
        ):
            from wave_server.engine.execution_manager import _run_execution

            await _run_execution(exec_id, seq_id)

        exc = await _get_execution(test_db, exec_id)
        assert exc.status == "completed"
        assert exc.completed_tasks == 5
        assert set(runner.spawned) == {"f1", "a1", "p1", "t1", "t2"}

    @pytest.mark.asyncio
    async def test_wave1_foundation_failure_no_features_no_wave2(
        self, test_db, _mock_storage, tmp_path
    ):
        """Foundation failure in wave 1 → no features, no wave 2."""
        plan = """\
# Implementation Plan
<!-- format: v2 -->
## Project Structure
```
src/
```

## Data Schemas
No schemas.

<!-- format: v2 -->
## Project Structure
```
src/
```

## Data Schemas
No schemas.


## Goal
Foundation failure cascade

## Wave 1: Setup

### Foundation
#### Task f1: Critical setup
- **Agent**: worker
- **Files**: `setup.py`
- **Depends**: (none)
- **Description**: Critical

### Feature: Auth
Files: `auth.py`

#### Task a1: Login
- **Agent**: worker
- **Files**: `auth.py`
- **Depends**: (none)
- **Description**: Login

## Wave 2: Tests


### Foundation

#### Task 2-1: Run tests
- **Files**: `test.py`
- **Depends**: (none)
- **Description**: Tests
"""
        _mock_storage.read_plan.return_value = plan
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()

        runner = MockRunner(results={"f1": 1})
        seq_id, exec_id = await _setup(test_db, plan, str(repo_dir))

        with patch(
            "wave_server.engine.execution_manager.get_runner", return_value=runner
        ):
            from wave_server.engine.execution_manager import _run_execution

            await _run_execution(exec_id, seq_id)

        exc = await _get_execution(test_db, exec_id)
        assert exc.status == "failed"
        assert "a1" not in runner.spawned
        assert "2-1" not in runner.spawned


# ═══════════════════════════════════════════════════════════════
# STRESS TESTS — high concurrency
# ═══════════════════════════════════════════════════════════════


class TestStressConcurrency:
    """Stress tests with many parallel tasks to exercise the DB lock."""

    @pytest.mark.asyncio
    async def test_eight_parallel_tasks(self, test_db, _mock_storage, tmp_path):
        """8 independent tasks running in parallel."""
        tasks = "\n".join(
            f"#### Task 1-{i}: Task{i}\n- **Files**: `f{i}.py`\n- **Depends**: (none)\n- **Description**: Task {i}\n"
            for i in range(1, 9)
        )
        plan = f"# Implementation Plan\n<!-- format: v2 -->\n\n## Project Structure\n```\nsrc/\n```\n\n## Data Schemas\nNo schemas.\n\n## Goal\nStress test\n\n## Wave 1: Parallel\n\n### Foundation\n\n{tasks}"
        _mock_storage.read_plan.return_value = plan
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()

        runner = MockRunner(default_delay=0.01)
        seq_id, exec_id = await _setup(test_db, plan, str(repo_dir), concurrency=8)

        with patch(
            "wave_server.engine.execution_manager.get_runner", return_value=runner
        ):
            from wave_server.engine.execution_manager import _run_execution

            await _run_execution(exec_id, seq_id)

        exc = await _get_execution(test_db, exec_id)
        assert exc.status == "completed", f"Expected completed, got {exc.status}"
        assert exc.completed_tasks == 8

        events = await _get_events(test_db, exec_id)
        assert [e.event_type for e in events].count("task_completed") == 8

    @pytest.mark.asyncio
    async def test_ten_parallel_instant_tasks(self, test_db, _mock_storage, tmp_path):
        """10 tasks completing instantly — maximum DB write contention."""
        tasks = "\n".join(
            f"#### Task 1-{i}: Task{i}\n- **Files**: `f{i}.py`\n- **Depends**: (none)\n- **Description**: Task {i}\n"
            for i in range(1, 11)
        )
        plan = f"# Implementation Plan\n<!-- format: v2 -->\n\n## Project Structure\n```\nsrc/\n```\n\n## Data Schemas\nNo schemas.\n\n## Goal\nInstant stress\n\n## Wave 1: Instant\n\n### Foundation\n\n{tasks}"
        _mock_storage.read_plan.return_value = plan
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()

        runner = MockRunner(default_delay=0)  # All instant
        seq_id, exec_id = await _setup(test_db, plan, str(repo_dir), concurrency=10)

        with patch(
            "wave_server.engine.execution_manager.get_runner", return_value=runner
        ):
            from wave_server.engine.execution_manager import _run_execution

            await _run_execution(exec_id, seq_id)

        exc = await _get_execution(test_db, exec_id)
        assert exc.status == "completed"
        assert exc.completed_tasks == 10

    @pytest.mark.asyncio
    async def test_three_waves_each_with_parallel_tasks(
        self, test_db, _mock_storage, tmp_path
    ):
        """3 waves, each with multiple parallel tasks."""
        waves_md = []
        total = 0
        for w in range(1, 4):
            task_count = w + 1  # 2, 3, 4 tasks per wave
            tasks = "\n".join(
                f"#### Task {w}-{t}: W{w}T{t}\n- **Files**: `w{w}t{t}.py`\n- **Depends**: (none)\n- **Description**: W{w}T{t}\n"
                for t in range(1, task_count + 1)
            )
            waves_md.append(f"## Wave {w}: Wave{w}\n\n### Foundation\n\n{tasks}")
            total += task_count

        plan = (
            "# Implementation Plan\n<!-- format: v2 -->\n\n## Project Structure\n```\nsrc/\n```\n\n## Data Schemas\nNo schemas.\n\n## Goal\nMulti-wave stress\n\n"
            + "\n".join(waves_md)
        )
        _mock_storage.read_plan.return_value = plan
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()

        runner = MockRunner(default_delay=0.01)
        seq_id, exec_id = await _setup(test_db, plan, str(repo_dir), concurrency=4)

        with patch(
            "wave_server.engine.execution_manager.get_runner", return_value=runner
        ):
            from wave_server.engine.execution_manager import _run_execution

            await _run_execution(exec_id, seq_id)

        exc = await _get_execution(test_db, exec_id)
        assert exc.status == "completed"
        assert exc.completed_tasks == total  # 2+3+4=9

        events = await _get_events(test_db, exec_id)
        wave_completed = [e for e in events if e.event_type == "wave_completed"]
        assert len(wave_completed) == 3
        assert all(json.loads(e.payload)["passed"] for e in wave_completed)


# ═══════════════════════════════════════════════════════════════
# DAG WITHIN CONCURRENT CONTEXT
# ═══════════════════════════════════════════════════════════════


class TestDAGWithinConcurrency:
    """Test that DAG ordering is correctly respected even under concurrency."""

    @pytest.mark.asyncio
    async def test_diamond_dag_in_foundation(self):
        """Diamond: A → B, A → C, B+C → D."""
        wave = Wave(
            name="Diamond",
            foundation=[
                _task("A"),
                _task("B", depends=["A"]),
                _task("C", depends=["A"]),
                _task("D", depends=["B", "C"]),
            ],
        )
        runner = MockRunner(default_delay=0.02)
        result = await execute_wave(
            WaveExecutorOptions(wave=wave, wave_num=1, runner=runner, max_concurrency=4)
        )
        assert result.passed

        # A must be first
        assert runner.spawn_times["A"] < runner.spawn_times["B"]
        assert runner.spawn_times["A"] < runner.spawn_times["C"]
        # B and C can run in parallel after A
        # D must wait for both B and C
        assert runner.completion_times["B"] < runner.spawn_times["D"]
        assert runner.completion_times["C"] < runner.spawn_times["D"]

    @pytest.mark.asyncio
    async def test_chain_dag_respects_order(self):
        """Linear chain: A → B → C → D → E."""
        wave = Wave(
            name="Chain",
            foundation=[
                _task("A"),
                _task("B", depends=["A"]),
                _task("C", depends=["B"]),
                _task("D", depends=["C"]),
                _task("E", depends=["D"]),
            ],
        )
        runner = MockRunner(default_delay=0.01)
        result = await execute_wave(
            WaveExecutorOptions(wave=wave, wave_num=1, runner=runner, max_concurrency=4)
        )
        assert result.passed

        for prev, curr in [("A", "B"), ("B", "C"), ("C", "D"), ("D", "E")]:
            assert runner.completion_times[prev] <= runner.spawn_times[curr], (
                f"{prev} should complete before {curr} starts"
            )

    @pytest.mark.asyncio
    async def test_wide_parallel_then_merge(self):
        """Fan-out then fan-in: A,B,C,D (parallel) → E (merge)."""
        wave = Wave(
            name="FanOut",
            foundation=[
                _task("A"),
                _task("B"),
                _task("C"),
                _task("D"),
                _task("E", depends=["A", "B", "C", "D"]),
            ],
        )
        runner = MockRunner(default_delay=0.02)
        result = await execute_wave(
            WaveExecutorOptions(wave=wave, wave_num=1, runner=runner, max_concurrency=4)
        )
        assert result.passed

        # E must wait for all of A, B, C, D
        for t in ["A", "B", "C", "D"]:
            assert runner.completion_times[t] <= runner.spawn_times["E"]

    @pytest.mark.asyncio
    async def test_dag_failure_cascades_to_dependents(self):
        """If a task fails, all its dependents should be skipped."""
        wave = Wave(
            name="Cascade",
            foundation=[
                _task("A"),
                _task("B", depends=["A"]),
                _task("C", depends=["B"]),
                _task("independent"),
            ],
        )
        runner = MockRunner(results={"A": 1}, default_delay=0.01)
        result = await execute_wave(
            WaveExecutorOptions(wave=wave, wave_num=1, runner=runner)
        )
        assert not result.passed

        # A ran and failed, B and C should be skipped
        assert "A" in runner.spawned
        assert "B" not in runner.spawned
        assert "C" not in runner.spawned
        # Independent task should still run
        assert "independent" in runner.spawned


# ═══════════════════════════════════════════════════════════════
# STORAGE ARTIFACTS UNDER CONCURRENCY
# ═══════════════════════════════════════════════════════════════


class TestStorageUnderConcurrency:
    """Verify storage.write_output / write_transcript called correctly
    for all parallel tasks."""

    @pytest.mark.asyncio
    async def test_output_written_for_all_parallel_tasks(
        self, test_db, _mock_storage, tmp_path
    ):
        """Each parallel task should get its output saved."""
        plan = """\
# Implementation Plan
<!-- format: v2 -->
## Project Structure
```
src/
```

## Data Schemas
No schemas.

<!-- format: v2 -->
## Project Structure
```
src/
```

## Data Schemas
No schemas.


## Goal
Storage test

## Wave 1: Work


### Foundation

#### Task 1-1: A
- **Files**: `a.py`
- **Depends**: (none)
- **Description**: A

#### Task 1-2: B
- **Files**: `b.py`
- **Depends**: (none)
- **Description**: B

#### Task 1-3: C
- **Files**: `c.py`
- **Depends**: (none)
- **Description**: C
"""
        _mock_storage.read_plan.return_value = plan
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()

        runner = MockRunner(default_delay=0.01)
        seq_id, exec_id = await _setup(test_db, plan, str(repo_dir))

        with patch(
            "wave_server.engine.execution_manager.get_runner", return_value=runner
        ):
            from wave_server.engine.execution_manager import _run_execution

            await _run_execution(exec_id, seq_id)

        # write_output should be called once per task
        assert _mock_storage.write_output.call_count == 3
        # write_transcript should be called once per task
        assert _mock_storage.write_transcript.call_count == 3

        # Verify task IDs in write_output calls
        output_task_ids = {
            call.args[1] for call in _mock_storage.write_output.call_args_list
        }
        assert output_task_ids == {"1-1", "1-2", "1-3"}

    @pytest.mark.asyncio
    async def test_log_flushed_after_each_task(self, test_db, _mock_storage, tmp_path):
        """Execution log should be flushed after each task completes."""
        plan = """\
# Implementation Plan
<!-- format: v2 -->
## Project Structure
```
src/
```

## Data Schemas
No schemas.

<!-- format: v2 -->
## Project Structure
```
src/
```

## Data Schemas
No schemas.


## Goal
Log flush test

## Wave 1: Work


### Foundation

#### Task 1-1: A
- **Files**: `a.py`
- **Depends**: (none)
- **Description**: A

#### Task 1-2: B
- **Files**: `b.py`
- **Depends**: (none)
- **Description**: B
"""
        _mock_storage.read_plan.return_value = plan
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()

        runner = MockRunner(default_delay=0.01)
        seq_id, exec_id = await _setup(test_db, plan, str(repo_dir))

        with patch(
            "wave_server.engine.execution_manager.get_runner", return_value=runner
        ):
            from wave_server.engine.execution_manager import _run_execution

            await _run_execution(exec_id, seq_id)

        # write_log is called for _flush_log — should be called multiple times
        # (at least: initial, per task start, per task end, wave start, wave end, final)
        assert _mock_storage.write_log.call_count >= 4


# ═══════════════════════════════════════════════════════════════
# EDGE CASES
# ═══════════════════════════════════════════════════════════════


class TestEdgeCases:
    """Edge cases for parallel execution and wave transitions."""

    @pytest.mark.asyncio
    async def test_single_task_single_wave(self, test_db, _mock_storage, tmp_path):
        """Minimal plan: 1 wave, 1 task."""
        plan = """\
# Implementation Plan
<!-- format: v2 -->
## Project Structure
```
src/
```

## Data Schemas
No schemas.

<!-- format: v2 -->
## Project Structure
```
src/
```

## Data Schemas
No schemas.


## Goal
Minimal

## Wave 1: Only


### Foundation

#### Task 1-1: Only task
- **Files**: `only.py`
- **Depends**: (none)
- **Description**: The only task
"""
        _mock_storage.read_plan.return_value = plan
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()

        runner = MockRunner()
        seq_id, exec_id = await _setup(test_db, plan, str(repo_dir))

        with patch(
            "wave_server.engine.execution_manager.get_runner", return_value=runner
        ):
            from wave_server.engine.execution_manager import _run_execution

            await _run_execution(exec_id, seq_id)

        exc = await _get_execution(test_db, exec_id)
        assert exc.status == "completed"
        assert exc.completed_tasks == 1
        assert exc.total_tasks == 1

    @pytest.mark.asyncio
    async def test_all_tasks_fail(self, test_db, _mock_storage, tmp_path):
        """All parallel foundation tasks fail."""
        plan = """\
# Implementation Plan
<!-- format: v2 -->
## Project Structure
```
src/
```

## Data Schemas
No schemas.

<!-- format: v2 -->
## Project Structure
```
src/
```

## Data Schemas
No schemas.


## Goal
All fail

## Wave 1: Doom

### Foundation
#### Task 1-1: Fail A
- **Files**: `a.py`
- **Depends**: (none)
- **Description**: A

#### Task 1-2: Fail B
- **Files**: `b.py`
- **Depends**: (none)
- **Description**: B
"""
        _mock_storage.read_plan.return_value = plan
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()

        runner = MockRunner(results={"1-1": 1, "1-2": 1}, default_delay=0.01)
        seq_id, exec_id = await _setup(test_db, plan, str(repo_dir))

        with patch(
            "wave_server.engine.execution_manager.get_runner", return_value=runner
        ):
            from wave_server.engine.execution_manager import _run_execution

            await _run_execution(exec_id, seq_id)

        exc = await _get_execution(test_db, exec_id)
        assert exc.status == "failed"

        events = await _get_events(test_db, exec_id)
        failed_count = sum(1 for e in events if e.event_type == "task_failed")
        assert failed_count == 2

    @pytest.mark.asyncio
    async def test_empty_wave_transitions_correctly(self):
        """A wave with no tasks should pass and allow the next wave to run."""
        waves = [
            Wave(name="Empty"),
            Wave(
                name="Real",
                features=[Feature(name="default", tasks=[_task("t1")])],
            ),
        ]
        runner = MockRunner()
        results = []
        for wave in waves:
            result = await execute_wave(
                WaveExecutorOptions(wave=wave, wave_num=1, runner=runner)
            )
            results.append(result)
            if not result.passed:
                break

        assert len(results) == 2
        assert results[0].passed  # Empty wave passes
        assert results[1].passed
        assert "t1" in runner.spawned

    @pytest.mark.asyncio
    async def test_runner_exception_during_parallel_execution(
        self, test_db, _mock_storage, tmp_path
    ):
        """If the runner throws during a parallel task, execution should fail gracefully."""
        plan = """\
# Implementation Plan
<!-- format: v2 -->
## Project Structure
```
src/
```

## Data Schemas
No schemas.

<!-- format: v2 -->
## Project Structure
```
src/
```

## Data Schemas
No schemas.


## Goal
Exception handling

## Wave 1: Work


### Foundation

#### Task 1-1: Normal
- **Files**: `a.py`
- **Depends**: (none)
- **Description**: Normal

#### Task 1-2: Explode
- **Files**: `b.py`
- **Depends**: (none)
- **Description**: Will throw
"""
        _mock_storage.read_plan.return_value = plan
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()

        class PartiallyExplodingRunner:
            spawned = []

            async def spawn(self, config: RunnerConfig) -> RunnerResult:
                self.spawned.append(config.task_id)
                if config.task_id == "1-2":
                    raise RuntimeError("Runner exploded on 1-2!")
                return RunnerResult(
                    exit_code=0,
                    stdout=json.dumps({"type": "result", "result": "ok"}),
                    stderr="",
                )

            def extract_final_output(self, stdout: str) -> str:
                try:
                    return json.loads(stdout).get("result", "")
                except (json.JSONDecodeError, AttributeError):
                    return stdout

        seq_id, exec_id = await _setup(test_db, plan, str(repo_dir))

        with patch(
            "wave_server.engine.execution_manager.get_runner",
            return_value=PartiallyExplodingRunner(),
        ):
            from wave_server.engine.execution_manager import _run_execution

            await _run_execution(exec_id, seq_id)

        exc = await _get_execution(test_db, exec_id)
        assert exc.status == "failed"
        assert exc.finished_at is not None

    @pytest.mark.asyncio
    async def test_git_sha_captured_across_waves(
        self, test_db, _mock_storage, tmp_path
    ):
        """Git SHA should be captured before and after execution."""
        plan = """\
# Implementation Plan
<!-- format: v2 -->
## Project Structure
```
src/
```

## Data Schemas
No schemas.

<!-- format: v2 -->
## Project Structure
```
src/
```

## Data Schemas
No schemas.


## Goal
Git SHA

## Wave 1: Work


### Foundation

#### Task 1-1: A
- **Files**: `a.py`
- **Depends**: (none)
- **Description**: A
"""
        _mock_storage.read_plan.return_value = plan
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()

        runner = MockRunner()
        seq_id, exec_id = await _setup(test_db, plan, str(repo_dir))

        with patch(
            "wave_server.engine.execution_manager.get_runner", return_value=runner
        ):
            from wave_server.engine.execution_manager import _run_execution

            await _run_execution(exec_id, seq_id)

        exc = await _get_execution(test_db, exec_id)
        assert exc.git_sha_before == "deadbeef"
        assert exc.git_sha_after == "deadbeef"

    @pytest.mark.asyncio
    async def test_finished_at_set_on_completion(
        self, test_db, _mock_storage, tmp_path
    ):
        """finished_at should be set when execution completes."""
        plan = """\
# Implementation Plan
<!-- format: v2 -->
## Project Structure
```
src/
```

## Data Schemas
No schemas.

<!-- format: v2 -->
## Project Structure
```
src/
```

## Data Schemas
No schemas.


## Goal
Timestamps

## Wave 1: Work


### Foundation

#### Task 1-1: A
- **Files**: `a.py`
- **Depends**: (none)
- **Description**: A
"""
        _mock_storage.read_plan.return_value = plan
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()

        runner = MockRunner()
        seq_id, exec_id = await _setup(test_db, plan, str(repo_dir))

        with patch(
            "wave_server.engine.execution_manager.get_runner", return_value=runner
        ):
            from wave_server.engine.execution_manager import _run_execution

            await _run_execution(exec_id, seq_id)

        exc = await _get_execution(test_db, exec_id)
        assert exc.started_at is not None
        assert exc.finished_at is not None
        assert exc.finished_at >= exc.started_at

    @pytest.mark.asyncio
    async def test_finished_at_set_on_failure(self, test_db, _mock_storage, tmp_path):
        """finished_at should be set even when execution fails."""
        plan = """\
# Implementation Plan
<!-- format: v2 -->
## Project Structure
```
src/
```

## Data Schemas
No schemas.

<!-- format: v2 -->
## Project Structure
```
src/
```

## Data Schemas
No schemas.


## Goal
Failure timestamps

## Wave 1: Work


### Foundation

#### Task 1-1: Fail
- **Files**: `a.py`
- **Depends**: (none)
- **Description**: Will fail
"""
        _mock_storage.read_plan.return_value = plan
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()

        runner = MockRunner(results={"1-1": 1})
        seq_id, exec_id = await _setup(test_db, plan, str(repo_dir))

        with patch(
            "wave_server.engine.execution_manager.get_runner", return_value=runner
        ):
            from wave_server.engine.execution_manager import _run_execution

            await _run_execution(exec_id, seq_id)

        exc = await _get_execution(test_db, exec_id)
        assert exc.status == "failed"
        assert exc.finished_at is not None
