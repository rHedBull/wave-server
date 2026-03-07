"""Integration tests for the execution manager — the full _run_execution lifecycle.

Tests the complete flow: DB setup → _run_execution → verify DB state + events.
Mocks: runner (no real claude), storage (no disk I/O), git SHA.
Uses: real SQLite in-memory DB, real plan parser, real wave executor.

Also tests the session concurrency bug: fire-and-forget asyncio.create_task
sharing a DB session causes "database is locked" with SQLite.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from wave_server.db import Base
from wave_server.engine.types import RunnerConfig, RunnerResult
from wave_server.models import (
    Event,
    Execution,
    Project,
    ProjectContextFile,
    ProjectRepository,
    Sequence,
)


# ── Test fixtures ──────────────────────────────────────────────


SIMPLE_PLAN = """\
# Implementation Plan
<!-- format: v2 -->
## Project Structure
```
src/
```

## Data Schemas
No schemas.


## Goal
Test execution

## Wave 1: Core


### Foundation

#### Task 1-1: First task
- **File**: `a.py`
- **Depends**: (none)
- **Description**: Do the first thing

#### Task 1-2: Second task
- **File**: `b.py`
- **Depends**: 1-1
- **Description**: Do the second thing
"""

TWO_WAVE_PLAN = """\
# Implementation Plan
<!-- format: v2 -->
## Project Structure
```
src/
```

## Data Schemas
No schemas.


## Goal
Multi-wave test

## Wave 1: Setup


### Foundation

#### Task 1-1: Setup task
- **File**: `setup.py`
- **Depends**: (none)
- **Description**: Set things up

## Wave 2: Build


### Foundation

#### Task 2-1: Build task
- **File**: `build.py`
- **Depends**: (none)
- **Description**: Build things
"""

INVALID_PLAN = """\
# Implementation Plan
<!-- format: v2 -->
## Project Structure
```
src/
```

## Data Schemas
No schemas.


## Goal
Bad plan

## Wave 1: Bad


### Foundation

#### Task 1-1: Depends on ghost
- **File**: `a.py`
- **Depends**: nonexistent
- **Description**: This will fail validation
"""


class MockRunner:
    """Mock runner with configurable per-task results and delays."""

    def __init__(
        self,
        results: dict[str, int] | None = None,
        delay_s: float = 0,
    ):
        self.results = results or {}
        self.delay_s = delay_s
        self.spawned: list[str] = []

    async def spawn(self, config: RunnerConfig) -> RunnerResult:
        self.spawned.append(config.task_id)
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        exit_code = self.results.get(config.task_id, 0)
        return RunnerResult(
            exit_code=exit_code,
            stdout=json.dumps({"type": "result", "result": f"Output for {config.task_id}"}),
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


@pytest_asyncio.fixture
async def test_db():
    """Create a fresh in-memory SQLite DB and patch async_session to use it."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    with patch("wave_server.engine.execution_manager.async_session", session_factory):
        yield session_factory

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def _setup_project_and_sequence(
    session_factory,
    repo_path: str | None = None,
    plan_content: str = SIMPLE_PLAN,
    context_files: list[tuple[str, str | None]] | None = None,
) -> tuple[str, str, str]:
    """Create project, repo, sequence, execution in DB. Returns (project_id, sequence_id, execution_id)."""
    async with session_factory() as db:
        project = Project(name="test-project")
        db.add(project)
        await db.commit()
        await db.refresh(project)

        if repo_path:
            repo = ProjectRepository(
                project_id=project.id, path=repo_path, label="test"
            )
            db.add(repo)
            await db.commit()

        if context_files:
            for path, desc in context_files:
                ctx = ProjectContextFile(
                    project_id=project.id, path=path, description=desc
                )
                db.add(ctx)
            await db.commit()

        sequence = Sequence(project_id=project.id, name="test-seq")
        db.add(sequence)
        await db.commit()
        await db.refresh(sequence)

        execution = Execution(
            sequence_id=sequence.id,
            runtime="claude",
            config=json.dumps({"concurrency": 2, "timeout_ms": 60000}),
        )
        db.add(execution)
        await db.commit()
        await db.refresh(execution)

        return project.id, sequence.id, execution.id


async def _get_execution(session_factory, execution_id: str) -> Execution:
    async with session_factory() as db:
        return await db.get(Execution, execution_id)


async def _get_events(session_factory, execution_id: str) -> list[Event]:
    async with session_factory() as db:
        result = await db.execute(
            select(Event)
            .where(Event.execution_id == execution_id)
            .order_by(Event.created_at)
        )
        return list(result.scalars().all())


# ── Patches applied to all tests ──────────────────────────────


@pytest.fixture(autouse=True)
def _mock_storage():
    """Mock all storage operations to avoid disk I/O."""
    with (
        patch("wave_server.engine.execution_manager.storage") as mock_storage,
    ):
        mock_storage.read_spec.return_value = "# Test Spec"
        mock_storage.read_plan.return_value = SIMPLE_PLAN
        mock_storage.write_output.return_value = None
        mock_storage.write_transcript.return_value = None
        mock_storage.append_log.return_value = None
        yield mock_storage


@pytest.fixture(autouse=True)
def _mock_git():
    """Mock git SHA capture."""
    with patch(
        "wave_server.engine.execution_manager._get_git_sha",
        return_value="abc123",
    ):
        yield


@pytest.fixture(autouse=True)
def _mock_runner():
    """Mock the runner factory to return our test runner."""
    runner = MockRunner()
    with patch(
        "wave_server.engine.execution_manager.get_runner",
        return_value=runner,
    ):
        yield runner


# ── Tests: No repo configured ─────────────────────────────────


class TestNoRepoConfigured:
    """Execution should fail fast when no repository is registered."""

    @pytest.mark.asyncio
    async def test_fails_immediately(self, test_db):
        _, seq_id, exec_id = await _setup_project_and_sequence(
            test_db, repo_path=None
        )
        from wave_server.engine.execution_manager import _run_execution

        await _run_execution(exec_id, seq_id)

        exc = await _get_execution(test_db, exec_id)
        assert exc.status == "failed"

    @pytest.mark.asyncio
    async def test_emits_error_event(self, test_db):
        _, seq_id, exec_id = await _setup_project_and_sequence(
            test_db, repo_path=None
        )
        from wave_server.engine.execution_manager import _run_execution

        await _run_execution(exec_id, seq_id)

        events = await _get_events(test_db, exec_id)
        run_completed = [e for e in events if e.event_type == "run_completed"]
        assert len(run_completed) == 1
        payload = json.loads(run_completed[0].payload)
        assert not payload["passed"]
        assert "repository" in payload["error"].lower()


# ── Tests: No plan ────────────────────────────────────────────


class TestNoPlan:
    """Execution should fail when no plan is uploaded."""

    @pytest.mark.asyncio
    async def test_fails_with_no_plan(self, test_db, _mock_storage, tmp_path):
        _mock_storage.read_plan.return_value = None
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        _, seq_id, exec_id = await _setup_project_and_sequence(
            test_db, repo_path=str(repo_dir)
        )
        from wave_server.engine.execution_manager import _run_execution

        await _run_execution(exec_id, seq_id)

        exc = await _get_execution(test_db, exec_id)
        assert exc.status == "failed"

        events = await _get_events(test_db, exec_id)
        completed = [e for e in events if e.event_type == "run_completed"]
        assert len(completed) == 1
        payload = json.loads(completed[0].payload)
        assert "No plan" in payload["error"]


# ── Tests: Invalid plan ───────────────────────────────────────


class TestInvalidPlan:
    """Execution should fail when plan has validation errors."""

    @pytest.mark.asyncio
    async def test_fails_with_invalid_deps(self, test_db, _mock_storage, tmp_path):
        _mock_storage.read_plan.return_value = INVALID_PLAN
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        _, seq_id, exec_id = await _setup_project_and_sequence(
            test_db, repo_path=str(repo_dir)
        )
        from wave_server.engine.execution_manager import _run_execution

        await _run_execution(exec_id, seq_id)

        exc = await _get_execution(test_db, exec_id)
        assert exc.status == "failed"

        events = await _get_events(test_db, exec_id)
        completed = [e for e in events if e.event_type == "run_completed"]
        payload = json.loads(completed[0].payload)
        assert "validation failed" in payload["error"].lower()


# ── Tests: Successful execution ───────────────────────────────


class TestSuccessfulExecution:
    """Full successful execution lifecycle."""

    @pytest.mark.asyncio
    async def test_status_transitions_to_completed(self, test_db, tmp_path):
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        _, seq_id, exec_id = await _setup_project_and_sequence(
            test_db, repo_path=str(repo_dir)
        )
        from wave_server.engine.execution_manager import _run_execution

        await _run_execution(exec_id, seq_id)
        # Drain fire-and-forget tasks
        await asyncio.sleep(0.1)

        exc = await _get_execution(test_db, exec_id)
        assert exc.status == "completed"
        assert exc.total_tasks == 2
        assert exc.completed_tasks == 2
        assert exc.git_sha_before == "abc123"
        assert exc.git_sha_after == "abc123"
        assert exc.started_at is not None
        assert exc.finished_at is not None

    @pytest.mark.asyncio
    async def test_all_events_emitted(self, test_db, tmp_path):
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        _, seq_id, exec_id = await _setup_project_and_sequence(
            test_db, repo_path=str(repo_dir)
        )
        from wave_server.engine.execution_manager import _run_execution

        await _run_execution(exec_id, seq_id)
        await asyncio.sleep(0.1)

        events = await _get_events(test_db, exec_id)
        event_types = [e.event_type for e in events]

        assert "run_started" in event_types
        assert "phase_changed" in event_types
        assert "wave_completed" in event_types
        assert "run_completed" in event_types
        # Task events (may be from fire-and-forget, so check they arrived)
        assert "task_started" in event_types
        assert "task_completed" in event_types

    @pytest.mark.asyncio
    async def test_run_completed_event_has_correct_payload(self, test_db, tmp_path):
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        _, seq_id, exec_id = await _setup_project_and_sequence(
            test_db, repo_path=str(repo_dir)
        )
        from wave_server.engine.execution_manager import _run_execution

        await _run_execution(exec_id, seq_id)
        await asyncio.sleep(0.1)

        events = await _get_events(test_db, exec_id)
        run_completed = [e for e in events if e.event_type == "run_completed"]
        assert len(run_completed) == 1
        payload = json.loads(run_completed[0].payload)
        assert payload["passed"] is True
        assert payload["total_tasks"] == 2
        assert payload["completed_tasks"] == 2
        assert "duration_ms" in payload

    @pytest.mark.asyncio
    async def test_waves_state_stored(self, test_db, tmp_path):
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        _, seq_id, exec_id = await _setup_project_and_sequence(
            test_db, repo_path=str(repo_dir)
        )
        from wave_server.engine.execution_manager import _run_execution

        await _run_execution(exec_id, seq_id)
        await asyncio.sleep(0.1)

        exc = await _get_execution(test_db, exec_id)
        assert exc.waves_state is not None
        ws = json.loads(exc.waves_state)
        assert ws["waves"][0]["passed"] is True

    @pytest.mark.asyncio
    async def test_storage_write_output_called(self, test_db, _mock_storage, tmp_path):
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        _, seq_id, exec_id = await _setup_project_and_sequence(
            test_db, repo_path=str(repo_dir)
        )
        from wave_server.engine.execution_manager import _run_execution

        await _run_execution(exec_id, seq_id)
        await asyncio.sleep(0.1)

        # write_output should have been called for each task
        assert _mock_storage.write_output.call_count >= 2

    @pytest.mark.asyncio
    async def test_storage_write_transcript_called(self, test_db, _mock_storage, tmp_path):
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        _, seq_id, exec_id = await _setup_project_and_sequence(
            test_db, repo_path=str(repo_dir)
        )
        from wave_server.engine.execution_manager import _run_execution

        await _run_execution(exec_id, seq_id)
        await asyncio.sleep(0.1)

        assert _mock_storage.write_transcript.call_count >= 2


# ── Tests: Failed task execution ──────────────────────────────


class TestFailedTaskExecution:
    """Execution with a failing task."""

    @pytest.mark.asyncio
    async def test_status_is_failed(self, test_db, _mock_runner, tmp_path):
        _mock_runner.results = {"1-1": 1}  # First task fails
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        _, seq_id, exec_id = await _setup_project_and_sequence(
            test_db, repo_path=str(repo_dir)
        )
        from wave_server.engine.execution_manager import _run_execution

        await _run_execution(exec_id, seq_id)
        await asyncio.sleep(0.1)

        exc = await _get_execution(test_db, exec_id)
        assert exc.status == "failed"

    @pytest.mark.asyncio
    async def test_task_failed_event_emitted(self, test_db, _mock_runner, tmp_path):
        _mock_runner.results = {"1-1": 1}
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        _, seq_id, exec_id = await _setup_project_and_sequence(
            test_db, repo_path=str(repo_dir)
        )
        from wave_server.engine.execution_manager import _run_execution

        await _run_execution(exec_id, seq_id)
        await asyncio.sleep(0.1)

        events = await _get_events(test_db, exec_id)
        failed_events = [e for e in events if e.event_type == "task_failed"]
        assert len(failed_events) >= 1
        assert failed_events[0].task_id == "1-1"

    @pytest.mark.asyncio
    async def test_wave_completed_with_passed_false(self, test_db, _mock_runner, tmp_path):
        _mock_runner.results = {"1-1": 1}
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        _, seq_id, exec_id = await _setup_project_and_sequence(
            test_db, repo_path=str(repo_dir)
        )
        from wave_server.engine.execution_manager import _run_execution

        await _run_execution(exec_id, seq_id)
        await asyncio.sleep(0.1)

        events = await _get_events(test_db, exec_id)
        wave_completed = [e for e in events if e.event_type == "wave_completed"]
        assert len(wave_completed) == 1
        payload = json.loads(wave_completed[0].payload)
        assert payload["passed"] is False

    @pytest.mark.asyncio
    async def test_second_task_skipped_on_dep_failure(self, test_db, _mock_runner, tmp_path):
        _mock_runner.results = {"1-1": 1}
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        _, seq_id, exec_id = await _setup_project_and_sequence(
            test_db, repo_path=str(repo_dir)
        )
        from wave_server.engine.execution_manager import _run_execution

        await _run_execution(exec_id, seq_id)
        await asyncio.sleep(0.1)

        # 1-2 depends on 1-1, so it should not have been spawned
        assert "1-2" not in _mock_runner.spawned


# ── Tests: Multi-wave execution ──────────────────────────────


class TestMultiWaveExecution:
    """Multi-wave execution with cross-wave state."""

    @pytest.mark.asyncio
    async def test_both_waves_execute(self, test_db, _mock_storage, tmp_path):
        _mock_storage.read_plan.return_value = TWO_WAVE_PLAN
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        _, seq_id, exec_id = await _setup_project_and_sequence(
            test_db, repo_path=str(repo_dir)
        )
        from wave_server.engine.execution_manager import _run_execution

        await _run_execution(exec_id, seq_id)
        await asyncio.sleep(0.1)

        exc = await _get_execution(test_db, exec_id)
        assert exc.status == "completed"
        assert exc.total_tasks == 2

        events = await _get_events(test_db, exec_id)
        phase_events = [e for e in events if e.event_type == "phase_changed"]
        assert len(phase_events) == 2  # One per wave

    @pytest.mark.asyncio
    async def test_wave1_failure_stops_wave2(self, test_db, _mock_storage, _mock_runner, tmp_path):
        _mock_storage.read_plan.return_value = TWO_WAVE_PLAN
        _mock_runner.results = {"1-1": 1}  # Wave 1 task fails
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        _, seq_id, exec_id = await _setup_project_and_sequence(
            test_db, repo_path=str(repo_dir)
        )
        from wave_server.engine.execution_manager import _run_execution

        await _run_execution(exec_id, seq_id)
        await asyncio.sleep(0.1)

        exc = await _get_execution(test_db, exec_id)
        assert exc.status == "failed"

        # Wave 2 task should not have been spawned
        assert "2-1" not in _mock_runner.spawned

        events = await _get_events(test_db, exec_id)
        wave_completed = [e for e in events if e.event_type == "wave_completed"]
        assert len(wave_completed) == 1  # Only wave 1


# ── Tests: Context file injection ─────────────────────────────


class TestContextFileInjection:
    """Test that context files are loaded and passed to the executor."""

    @pytest.mark.asyncio
    async def test_context_loaded_into_runner(self, test_db, _mock_storage, tmp_path):
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        (repo_dir / "ARCH.md").write_text("# Architecture\nMicroservices pattern")

        _, seq_id, exec_id = await _setup_project_and_sequence(
            test_db,
            repo_path=str(repo_dir),
            context_files=[("ARCH.md", "Architecture doc")],
        )

        prompts_seen = []
        original_runner = MockRunner()

        class CapturingRunner:
            async def spawn(self, config: RunnerConfig) -> RunnerResult:
                prompts_seen.append(config.prompt)
                return await original_runner.spawn(config)

            def extract_final_output(self, stdout: str) -> str:
                return original_runner.extract_final_output(stdout)

        with patch(
            "wave_server.engine.execution_manager.get_runner",
            return_value=CapturingRunner(),
        ):
            from wave_server.engine.execution_manager import _run_execution
            await _run_execution(exec_id, seq_id)
            await asyncio.sleep(0.1)

        # At least one prompt should contain the context
        assert any("Microservices" in p for p in prompts_seen)
        assert any("Architecture" in p for p in prompts_seen)


# ── Tests: Session concurrency (the database locked bug) ──────


class TestSessionConcurrency:
    """Tests around the fire-and-forget asyncio.create_task pattern.

    The current implementation has a bug: on_task_start and on_task_end
    use asyncio.create_task(_emit_event(db, ...)) which shares the main
    session across concurrent tasks. With SQLite this causes
    "database is locked" errors.

    These tests verify that events are correctly recorded even with
    the concurrent writes, and expose scenarios where the bug manifests.
    """

    @pytest.mark.asyncio
    async def test_events_arrive_after_sleep(self, test_db, tmp_path):
        """Events from fire-and-forget tasks should arrive if we wait."""
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        _, seq_id, exec_id = await _setup_project_and_sequence(
            test_db, repo_path=str(repo_dir)
        )
        from wave_server.engine.execution_manager import _run_execution

        await _run_execution(exec_id, seq_id)
        # Give fire-and-forget tasks time to complete
        await asyncio.sleep(0.2)

        events = await _get_events(test_db, exec_id)
        event_types = [e.event_type for e in events]

        # These are emitted via asyncio.create_task (fire-and-forget)
        task_started_count = event_types.count("task_started")
        task_completed_count = event_types.count("task_completed")

        # With 2 tasks, we expect 2 started + 2 completed
        assert task_started_count == 2, f"Expected 2 task_started, got {task_started_count}"
        assert task_completed_count == 2, f"Expected 2 task_completed, got {task_completed_count}"

    @pytest.mark.asyncio
    async def test_completed_count_updates(self, test_db, tmp_path):
        """_update_completed_count opens its own session — verify final count."""
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        _, seq_id, exec_id = await _setup_project_and_sequence(
            test_db, repo_path=str(repo_dir)
        )
        from wave_server.engine.execution_manager import _run_execution

        await _run_execution(exec_id, seq_id)
        await asyncio.sleep(0.2)

        exc = await _get_execution(test_db, exec_id)
        assert exc.completed_tasks == 2

    @pytest.mark.asyncio
    async def test_concurrent_tasks_no_lost_events(self, test_db, _mock_storage, tmp_path):
        """With many parallel tasks, no events should be lost.

        This tests a plan with multiple independent tasks in wave 1
        that all run concurrently, stressing the concurrent session pattern.
        """
        many_tasks_plan = """\
# Implementation Plan
<!-- format: v2 -->
## Project Structure
```
src/
```

## Data Schemas
No schemas.


## Goal
Stress test concurrent events

## Wave 1: Parallel


### Foundation

#### Task 1-1: Task A
- **File**: `a.py`
- **Depends**: (none)
- **Description**: A

#### Task 1-2: Task B
- **File**: `b.py`
- **Depends**: (none)
- **Description**: B

#### Task 1-3: Task C
- **File**: `c.py`
- **Depends**: (none)
- **Description**: C

#### Task 1-4: Task D
- **File**: `d.py`
- **Depends**: (none)
- **Description**: D
"""
        _mock_storage.read_plan.return_value = many_tasks_plan
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        _, seq_id, exec_id = await _setup_project_and_sequence(
            test_db, repo_path=str(repo_dir)
        )
        from wave_server.engine.execution_manager import _run_execution

        await _run_execution(exec_id, seq_id)
        await asyncio.sleep(0.5)  # Extra time for concurrent writes

        events = await _get_events(test_db, exec_id)
        event_types = [e.event_type for e in events]

        task_started_count = event_types.count("task_started")
        task_completed_count = event_types.count("task_completed")

        # All 4 tasks should have started and completed events
        assert task_started_count == 4, (
            f"Expected 4 task_started, got {task_started_count}. "
            f"Events: {event_types}"
        )
        assert task_completed_count == 4, (
            f"Expected 4 task_completed, got {task_completed_count}. "
            f"Events: {event_types}"
        )

    @pytest.mark.asyncio
    async def test_fire_and_forget_events_with_slow_tasks(self, test_db, _mock_storage, tmp_path):
        """Slow tasks mean more time between fire-and-forget DB writes."""
        _mock_storage.read_plan.return_value = SIMPLE_PLAN
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()

        slow_runner = MockRunner(delay_s=0.05)
        _, seq_id, exec_id = await _setup_project_and_sequence(
            test_db, repo_path=str(repo_dir)
        )

        with patch(
            "wave_server.engine.execution_manager.get_runner",
            return_value=slow_runner,
        ):
            from wave_server.engine.execution_manager import _run_execution
            await _run_execution(exec_id, seq_id)
            await asyncio.sleep(0.3)

        events = await _get_events(test_db, exec_id)
        event_types = [e.event_type for e in events]

        assert event_types.count("task_started") == 2
        assert event_types.count("task_completed") == 2

    @pytest.mark.asyncio
    async def test_event_order_is_logical(self, test_db, tmp_path):
        """Events should follow a logical order even with async emission."""
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        _, seq_id, exec_id = await _setup_project_and_sequence(
            test_db, repo_path=str(repo_dir)
        )
        from wave_server.engine.execution_manager import _run_execution

        await _run_execution(exec_id, seq_id)
        await asyncio.sleep(0.2)

        events = await _get_events(test_db, exec_id)
        event_types = [e.event_type for e in events]

        # run_started should be first
        assert event_types[0] == "run_started"
        # run_completed should be last
        assert event_types[-1] == "run_completed"
        # phase_changed before any task events
        phase_idx = event_types.index("phase_changed")
        first_task_idx = next(
            i for i, t in enumerate(event_types) if t.startswith("task_")
        )
        assert phase_idx < first_task_idx


# ── Tests: Exception handling ─────────────────────────────────


class TestExceptionHandling:
    """Test that unexpected exceptions are caught and execution marked failed."""

    @pytest.mark.asyncio
    async def test_runner_exception_marks_failed(self, test_db, tmp_path):
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        _, seq_id, exec_id = await _setup_project_and_sequence(
            test_db, repo_path=str(repo_dir)
        )

        class ExplodingRunner:
            async def spawn(self, config: RunnerConfig) -> RunnerResult:
                raise RuntimeError("Runner exploded!")

            def extract_final_output(self, stdout: str) -> str:
                return ""

        with patch(
            "wave_server.engine.execution_manager.get_runner",
            return_value=ExplodingRunner(),
        ):
            from wave_server.engine.execution_manager import _run_execution
            await _run_execution(exec_id, seq_id)

        exc = await _get_execution(test_db, exec_id)
        assert exc.status == "failed"
        assert exc.finished_at is not None

    @pytest.mark.asyncio
    async def test_exception_emits_error_event(self, test_db, tmp_path):
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        _, seq_id, exec_id = await _setup_project_and_sequence(
            test_db, repo_path=str(repo_dir)
        )

        class ExplodingRunner:
            async def spawn(self, config: RunnerConfig) -> RunnerResult:
                raise RuntimeError("Boom!")

            def extract_final_output(self, stdout: str) -> str:
                return ""

        with patch(
            "wave_server.engine.execution_manager.get_runner",
            return_value=ExplodingRunner(),
        ):
            from wave_server.engine.execution_manager import _run_execution
            await _run_execution(exec_id, seq_id)

        events = await _get_events(test_db, exec_id)
        error_events = [
            e for e in events
            if e.event_type == "run_completed"
            and "Boom" in json.loads(e.payload).get("error", "")
        ]
        assert len(error_events) == 1


# ── Tests: Nonexistent execution/sequence ─────────────────────


class TestMissingRecords:
    """Edge cases: execution or sequence doesn't exist."""

    @pytest.mark.asyncio
    async def test_nonexistent_execution_returns_silently(self, test_db):
        from wave_server.engine.execution_manager import _run_execution
        # Should not raise
        await _run_execution("nonexistent-id", "nonexistent-seq")

    @pytest.mark.asyncio
    async def test_nonexistent_sequence_returns_silently(self, test_db):
        async with test_db() as db:
            project = Project(name="p")
            db.add(project)
            await db.commit()
            await db.refresh(project)
            seq = Sequence(project_id=project.id, name="s")
            db.add(seq)
            await db.commit()
            await db.refresh(seq)
            exc = Execution(sequence_id=seq.id, runtime="claude", config="{}")
            db.add(exc)
            await db.commit()
            await db.refresh(exc)
            exec_id = exc.id

        from wave_server.engine.execution_manager import _run_execution
        # Pass valid execution but wrong sequence
        await _run_execution(exec_id, "wrong-sequence-id")


# ── Tests: Continuation (continue_from) ──────────────────────


class TestContinuation:
    """Test _run_execution with continue_from — skipping completed tasks."""

    @pytest.mark.asyncio
    async def test_get_completed_task_ids(self, test_db):
        """_get_completed_task_ids returns only IDs from task_completed events."""
        from wave_server.engine.execution_manager import _get_completed_task_ids

        async with test_db() as db:
            # Seed some events for an execution
            exec_id = "test-exec-001"
            events = [
                Event(execution_id=exec_id, event_type="task_started", task_id="t1"),
                Event(execution_id=exec_id, event_type="task_completed", task_id="t1"),
                Event(execution_id=exec_id, event_type="task_started", task_id="t2"),
                Event(execution_id=exec_id, event_type="task_failed", task_id="t2"),
                Event(execution_id=exec_id, event_type="task_started", task_id="t3"),
                Event(execution_id=exec_id, event_type="task_completed", task_id="t3"),
                Event(execution_id=exec_id, event_type="run_completed"),
            ]
            db.add_all(events)
            await db.commit()

            completed = await _get_completed_task_ids(db, exec_id)

        assert completed == {"t1", "t3"}

    @pytest.mark.asyncio
    async def test_get_completed_task_ids_empty(self, test_db):
        """Returns empty set when no tasks completed."""
        from wave_server.engine.execution_manager import _get_completed_task_ids

        async with test_db() as db:
            exec_id = "test-exec-empty"
            events = [
                Event(execution_id=exec_id, event_type="task_started", task_id="t1"),
                Event(execution_id=exec_id, event_type="task_failed", task_id="t1"),
            ]
            db.add_all(events)
            await db.commit()

            completed = await _get_completed_task_ids(db, exec_id)

        assert completed == set()

    @pytest.mark.asyncio
    async def test_continue_skips_completed_tasks(self, test_db, tmp_path):
        """_run_execution with continue_from skips tasks completed in prior execution."""
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()

        # Run 1: task 1-1 succeeds, 1-2 fails
        runner1 = MockRunner(results={"1-2": 1})
        with patch(
            "wave_server.engine.execution_manager.get_runner",
            return_value=runner1,
        ):
            _, seq_id, exec1_id = await _setup_project_and_sequence(
                test_db, repo_path=str(repo_dir)
            )
            from wave_server.engine.execution_manager import _run_execution

            await _run_execution(exec1_id, seq_id)
            await asyncio.sleep(0.1)

        exc1 = await _get_execution(test_db, exec1_id)
        assert exc1.status == "failed"
        assert "1-1" in runner1.spawned

        # Run 2: continue from run 1
        runner2 = MockRunner()
        with patch(
            "wave_server.engine.execution_manager.get_runner",
            return_value=runner2,
        ):
            async with test_db() as db:
                exec2 = Execution(
                    sequence_id=seq_id,
                    continued_from=exec1_id,
                    trigger="continuation",
                    runtime="claude",
                    config=json.dumps({"concurrency": 2, "timeout_ms": 60000}),
                )
                db.add(exec2)
                await db.commit()
                await db.refresh(exec2)
                exec2_id = exec2.id

            await _run_execution(exec2_id, seq_id, continue_from=exec1_id)
            await asyncio.sleep(0.1)

        exc2 = await _get_execution(test_db, exec2_id)
        assert exc2.status == "completed"

        # 1-1 was completed in run 1 → skipped in run 2
        assert "1-1" not in runner2.spawned
        # 1-2 failed in run 1 → re-run in run 2
        assert "1-2" in runner2.spawned

    @pytest.mark.asyncio
    async def test_continue_events_include_skipped_tasks(self, test_db, tmp_path):
        """Resumed tasks still emit task_completed events in the new execution."""
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()

        # Run 1: 1-1 succeeds, 1-2 fails
        runner1 = MockRunner(results={"1-2": 1})
        with patch(
            "wave_server.engine.execution_manager.get_runner",
            return_value=runner1,
        ):
            _, seq_id, exec1_id = await _setup_project_and_sequence(
                test_db, repo_path=str(repo_dir)
            )
            from wave_server.engine.execution_manager import _run_execution

            await _run_execution(exec1_id, seq_id)
            await asyncio.sleep(0.1)

        # Run 2: continue
        runner2 = MockRunner()
        with patch(
            "wave_server.engine.execution_manager.get_runner",
            return_value=runner2,
        ):
            async with test_db() as db:
                exec2 = Execution(
                    sequence_id=seq_id,
                    continued_from=exec1_id,
                    trigger="continuation",
                    runtime="claude",
                    config=json.dumps({"concurrency": 2, "timeout_ms": 60000}),
                )
                db.add(exec2)
                await db.commit()
                await db.refresh(exec2)
                exec2_id = exec2.id

            await _run_execution(exec2_id, seq_id, continue_from=exec1_id)
            await asyncio.sleep(0.1)

        # The new execution should have task_completed events for both tasks
        events = await _get_events(test_db, exec2_id)
        completed_task_ids = [
            e.task_id for e in events if e.event_type == "task_completed"
        ]
        assert "1-1" in completed_task_ids  # resumed (skipped)
        assert "1-2" in completed_task_ids  # actually re-run

    @pytest.mark.asyncio
    async def test_continue_with_nonexistent_parent_runs_all(self, test_db, tmp_path):
        """If continue_from references a bogus ID, no tasks are skipped."""
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()

        runner = MockRunner()
        with patch(
            "wave_server.engine.execution_manager.get_runner",
            return_value=runner,
        ):
            _, seq_id, exec_id = await _setup_project_and_sequence(
                test_db, repo_path=str(repo_dir)
            )
            from wave_server.engine.execution_manager import _run_execution

            await _run_execution(exec_id, seq_id, continue_from="nonexistent")
            await asyncio.sleep(0.1)

        exc = await _get_execution(test_db, exec_id)
        assert exc.status == "completed"
        # All tasks should have been spawned (nothing to skip)
        assert "1-1" in runner.spawned
        assert "1-2" in runner.spawned
