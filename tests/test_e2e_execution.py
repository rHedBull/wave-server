"""End-to-end execution workflow test.

Exercises the complete lifecycle through the REST API with a mock runner:
  1. Create project
  2. Register repository
  3. Create sequence
  4. Upload plan (markdown)
  5. Start execution (with mock runner injected)
  6. Poll until completion
  7. Verify: execution status, events, tasks, output, log artifacts

This is the automated "capability test" — proves the whole pipeline
works without needing a real claude CLI.
"""

from __future__ import annotations

import asyncio
import json
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from wave_server.db import Base, get_db
from wave_server.engine.runner import AgentRunner
from wave_server.engine.types import RunnerConfig, RunnerResult
from wave_server.main import app


# ── Mock Runner ────────────────────────────────────────────────


class E2EMockRunner:
    """Mock runner that simulates successful task execution.

    Records every spawn call and returns configurable results per task ID.
    Default: all tasks succeed with a simulated output.
    """

    def __init__(
        self,
        results: dict[str, int] | None = None,
        delay_s: float = 0.01,
    ):
        self.results = results or {}  # task_id -> exit_code
        self.delay_s = delay_s
        self.spawned: list[str] = []
        self.prompts: dict[str, str] = {}

    async def spawn(self, config: RunnerConfig) -> RunnerResult:
        self.spawned.append(config.task_id)
        self.prompts[config.task_id] = config.prompt
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        exit_code = self.results.get(config.task_id, 0)
        output = f"Completed task {config.task_id} successfully"
        return RunnerResult(
            exit_code=exit_code,
            stdout=json.dumps({"type": "result", "result": output}),
            stderr="" if exit_code == 0 else f"Task {config.task_id} failed",
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


# Verify protocol compliance
assert isinstance(E2EMockRunner(), AgentRunner)


# ── Test Plans ─────────────────────────────────────────────────


SIMPLE_PLAN = textwrap.dedent("""\
    # Implementation Plan
    <!-- format: v2 -->

    ## Project Structure
    ```
    src/
    ```

    ## Data Schemas
    No schemas.

    ## Goal
    Build a simple greeting module.

    ## Wave 1: Core Setup

    ### Foundation

    #### Task 1-1: Create greeting module
    - **Agent**: worker
    - **Files**: `src/greet.py`
    - **Depends**: (none)
    - **Description**: Create a Python greeting module with a `hello(name)` function.

    #### Task 1-2: Add tests
    - **Agent**: test-writer
    - **Files**: `tests/test_greet.py`
    - **Depends**: 1-1
    - **Description**: Write tests for the greeting module.

    #### Task 1-3: Verify
    - **Agent**: wave-verifier
    - **Files**: `src/greet.py`, `tests/test_greet.py`
    - **Depends**: 1-2
    - **Description**: Verify the greeting module and tests are correct.
""")

MULTI_WAVE_PLAN = textwrap.dedent("""\
    # Implementation Plan
    <!-- format: v2 -->

    ## Project Structure
    ```
    src/
    ```

    ## Data Schemas
    No schemas.

    ## Goal
    Build a user auth system.

    ## Wave 1: Foundation

    ### Foundation

    #### Task 1-f1: Project setup
    - **Agent**: worker
    - **Files**: `setup.py`, `requirements.txt`
    - **Depends**: (none)
    - **Description**: Initialize project structure and dependencies.

    ### Feature: Auth

    #### Task 1-a1: User model
    - **Agent**: worker
    - **Files**: `src/models/user.py`
    - **Depends**: (none)
    - **Description**: Create the User database model.

    #### Task 1-a2: Auth service
    - **Agent**: worker
    - **Files**: `src/services/auth.py`
    - **Depends**: 1-a1
    - **Description**: Implement authentication service.

    ### Feature: Profile

    #### Task 1-p1: Profile model
    - **Agent**: worker
    - **Files**: `src/models/profile.py`
    - **Depends**: (none)
    - **Description**: Create the Profile model.

    ### Integration

    #### Task 1-i1: Integration tests
    - **Agent**: wave-verifier
    - **Files**: `tests/test_integration.py`
    - **Depends**: (none)
    - **Description**: Verify auth and profile features work together.

    ## Wave 2: API Layer

    ### Feature: API

    #### Task 2-1: Auth endpoints
    - **Agent**: worker
    - **Files**: `src/routes/auth.py`
    - **Depends**: (none)
    - **Description**: Create REST API endpoints for authentication.

    #### Task 2-2: Profile endpoints
    - **Agent**: worker
    - **Files**: `src/routes/profile.py`
    - **Depends**: 2-1
    - **Description**: Create REST API endpoints for profiles.
""")

FAILING_TASK_PLAN = textwrap.dedent("""\
    # Implementation Plan
    <!-- format: v2 -->

    ## Project Structure
    ```
    src/
    ```

    ## Data Schemas
    No schemas.

    ## Goal
    A plan where one task fails.

    ## Wave 1: Tasks

    ### Foundation

    #### Task 1-1: Succeeds
    - **Agent**: worker
    - **Files**: `src/ok.py`
    - **Depends**: (none)
    - **Description**: This task succeeds.

    #### Task 1-2: Fails
    - **Agent**: worker
    - **Files**: `src/fail.py`
    - **Depends**: 1-1
    - **Description**: This task fails.

    #### Task 1-3: Skipped
    - **Agent**: worker
    - **Files**: `src/skipped.py`
    - **Depends**: 1-2
    - **Description**: This task should be skipped due to 1-2 failing.
""")


# ── Fixtures ───────────────────────────────────────────────────


@pytest_asyncio.fixture
async def e2e_client(tmp_path: Path):
    """Test client with a shared in-memory DB for both API and background tasks.

    Overrides both the FastAPI dependency *and* the module-level async_session
    used by execution_manager, so the background execution task writes to the
    same in-memory database the API reads from.
    """
    import wave_server.db as db_mod

    # Use file-based SQLite so both the API sessions and the background
    # execution task (which opens its own session) see the same data.
    # In-memory SQLite can't share across connections without StaticPool,
    # and StaticPool deadlocks with concurrent access.
    db_path = tmp_path / "test.db"
    test_engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    test_session_factory = async_sessionmaker(test_engine, expire_on_commit=False)

    # Override the FastAPI dependency
    async def override_get_db():
        async with test_session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db

    # Override the module-level async_session used by execution_manager.
    # execution_manager does `from wave_server.db import async_session` so
    # it has its own reference — we must patch it there too.
    import wave_server.engine.execution_manager as em_mod
    original_async_session_db = db_mod.async_session
    original_async_session_em = em_mod.async_session
    db_mod.async_session = test_session_factory
    em_mod.async_session = test_session_factory

    # Point storage at tmp_path so artifacts don't pollute real data
    from wave_server.config import settings
    original_data_dir = settings.data_dir
    settings.data_dir = tmp_path / "data"
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    settings.data_dir = original_data_dir
    db_mod.async_session = original_async_session_db
    em_mod.async_session = original_async_session_em
    app.dependency_overrides.clear()
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await test_engine.dispose()


@pytest.fixture
def repo_dir(tmp_path: Path) -> Path:
    """Create a fake repository directory for project registration."""
    d = tmp_path / "repo"
    d.mkdir()
    # Make it look like a git repo (for git_sha capture)
    (d / ".git").mkdir()
    return d


# ── Helpers ────────────────────────────────────────────────────


async def _create_project(client: AsyncClient) -> str:
    """Create a project and return its ID."""
    r = await client.post("/api/v1/projects", json={"name": "e2e-test-project"})
    assert r.status_code == 201
    return r.json()["id"]


async def _register_repo(client: AsyncClient, project_id: str, repo_path: Path) -> str:
    """Register a repository and return its ID."""
    r = await client.post(
        f"/api/v1/projects/{project_id}/repositories",
        json={"path": str(repo_path), "label": "test-repo"},
    )
    assert r.status_code == 201
    return r.json()["id"]


async def _create_sequence(client: AsyncClient, project_id: str, name: str = "test-seq") -> str:
    """Create a sequence and return its ID."""
    r = await client.post(
        f"/api/v1/projects/{project_id}/sequences",
        json={"name": name},
    )
    assert r.status_code == 201
    return r.json()["id"]


async def _upload_plan(client: AsyncClient, sequence_id: str, plan_md: str) -> None:
    """Upload a plan markdown to a sequence."""
    r = await client.post(
        f"/api/v1/sequences/{sequence_id}/plan",
        content=plan_md,
        headers={"Content-Type": "text/plain"},
    )
    assert r.status_code in (200, 204), f"Plan upload failed: {r.status_code} {r.text}"


async def _start_execution(client: AsyncClient, sequence_id: str) -> str:
    """Start an execution and return its ID."""
    r = await client.post(
        f"/api/v1/sequences/{sequence_id}/executions",
        json={},
    )
    assert r.status_code == 201
    return r.json()["id"]


async def _poll_execution(
    client: AsyncClient,
    execution_id: str,
    timeout_s: float = 10.0,
    interval_s: float = 0.1,
) -> dict:
    """Poll an execution until it reaches a terminal state."""
    terminal = {"completed", "failed", "cancelled"}
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        r = await client.get(f"/api/v1/executions/{execution_id}")
        assert r.status_code == 200
        data = r.json()
        if data["status"] in terminal:
            return data
        await asyncio.sleep(interval_s)
    raise TimeoutError(f"Execution {execution_id} did not complete within {timeout_s}s")


# ── Tests ──────────────────────────────────────────────────────


class TestE2ESimplePlan:
    """End-to-end test with a simple 3-task single-wave plan."""

    @pytest.mark.asyncio
    async def test_full_happy_path(self, e2e_client: AsyncClient, repo_dir: Path):
        """Complete workflow: project → repo → sequence → plan → execute → verify."""
        client = e2e_client
        mock_runner = E2EMockRunner()

        # 1. Setup: project + repo + sequence + plan
        project_id = await _create_project(client)
        await _register_repo(client, project_id, repo_dir)
        sequence_id = await _create_sequence(client, project_id)
        await _upload_plan(client, sequence_id, SIMPLE_PLAN)

        # 2. Start execution with mock runner
        with patch(
            "wave_server.engine.execution_manager.get_runner",
            return_value=mock_runner,
        ):
            execution_id = await _start_execution(client, sequence_id)
            result = await _poll_execution(client, execution_id)

        # 3. Verify execution completed successfully
        assert result["status"] == "completed", f"Expected completed, got {result['status']}"
        assert result["total_tasks"] == 3
        assert result["completed_tasks"] == 3

        # 4. Verify all tasks were spawned
        assert set(mock_runner.spawned) == {"1-1", "1-2", "1-3"}

        # 5. Verify events were emitted
        r = await client.get(f"/api/v1/executions/{execution_id}/events")
        assert r.status_code == 200
        events = r.json()
        event_types = [e["event_type"] for e in events]
        assert "run_started" in event_types
        assert "run_completed" in event_types
        assert event_types.count("task_started") == 3
        assert event_types.count("task_completed") == 3

        # 6. Verify task summary
        r = await client.get(f"/api/v1/executions/{execution_id}/tasks")
        assert r.status_code == 200
        tasks = r.json()
        assert len(tasks) == 3
        assert all(t["status"] == "completed" for t in tasks)

        # 7. Verify task output artifacts exist
        for task_id in ["1-1", "1-2", "1-3"]:
            r = await client.get(f"/api/v1/executions/{execution_id}/output/{task_id}")
            assert r.status_code == 200
            assert f"Completed task {task_id}" in r.text

        # 8. Verify execution log exists
        r = await client.get(f"/api/v1/executions/{execution_id}/log")
        assert r.status_code == 200
        assert len(r.text) > 0

    @pytest.mark.asyncio
    async def test_prompts_contain_task_details(self, e2e_client: AsyncClient, repo_dir: Path):
        """Verify that prompts passed to the runner contain task metadata."""
        client = e2e_client
        mock_runner = E2EMockRunner()

        project_id = await _create_project(client)
        await _register_repo(client, project_id, repo_dir)
        sequence_id = await _create_sequence(client, project_id)
        await _upload_plan(client, sequence_id, SIMPLE_PLAN)

        with patch(
            "wave_server.engine.execution_manager.get_runner",
            return_value=mock_runner,
        ):
            execution_id = await _start_execution(client, sequence_id)
            await _poll_execution(client, execution_id)

        # Worker task gets implementation prompt
        assert "implementing code" in mock_runner.prompts["1-1"].lower()
        assert "src/greet.py" in mock_runner.prompts["1-1"]

        # Test writer gets test prompt
        assert "writing tests" in mock_runner.prompts["1-2"].lower()
        assert "tests/test_greet.py" in mock_runner.prompts["1-2"]

        # Verifier gets verification prompt
        assert "verifying" in mock_runner.prompts["1-3"].lower()
        assert "Do NOT modify" in mock_runner.prompts["1-3"]


class TestE2EMultiWavePlan:
    """End-to-end test with a multi-wave, multi-feature plan."""

    @pytest.mark.asyncio
    async def test_multi_wave_all_pass(self, e2e_client: AsyncClient, repo_dir: Path):
        """Two waves with foundation, features, and integration all pass."""
        client = e2e_client
        mock_runner = E2EMockRunner()

        project_id = await _create_project(client)
        await _register_repo(client, project_id, repo_dir)
        sequence_id = await _create_sequence(client, project_id, "auth-system")
        await _upload_plan(client, sequence_id, MULTI_WAVE_PLAN)

        with patch(
            "wave_server.engine.execution_manager.get_runner",
            return_value=mock_runner,
        ):
            execution_id = await _start_execution(client, sequence_id)
            result = await _poll_execution(client, execution_id)

        assert result["status"] == "completed"

        # All tasks from both waves should have been spawned
        expected_tasks = {"1-f1", "1-a1", "1-a2", "1-p1", "1-i1", "2-1", "2-2"}
        assert set(mock_runner.spawned) == expected_tasks

        # Verify events include wave transitions
        r = await client.get(f"/api/v1/executions/{execution_id}/events")
        events = r.json()
        event_types = [e["event_type"] for e in events]
        assert event_types.count("wave_completed") == 2
        assert event_types.count("phase_changed") == 2

        # Verify task count
        assert result["total_tasks"] == 7
        assert result["completed_tasks"] == 7

    @pytest.mark.asyncio
    async def test_dependency_ordering(self, e2e_client: AsyncClient, repo_dir: Path):
        """Verify that dependent tasks run after their dependencies."""
        client = e2e_client
        mock_runner = E2EMockRunner(delay_s=0.02)  # Small delay to enforce ordering

        project_id = await _create_project(client)
        await _register_repo(client, project_id, repo_dir)
        sequence_id = await _create_sequence(client, project_id)
        await _upload_plan(client, sequence_id, SIMPLE_PLAN)

        with patch(
            "wave_server.engine.execution_manager.get_runner",
            return_value=mock_runner,
        ):
            execution_id = await _start_execution(client, sequence_id)
            await _poll_execution(client, execution_id)

        # In the simple plan: 1-1 → 1-2 → 1-3
        idx_1_1 = mock_runner.spawned.index("1-1")
        idx_1_2 = mock_runner.spawned.index("1-2")
        idx_1_3 = mock_runner.spawned.index("1-3")
        assert idx_1_1 < idx_1_2 < idx_1_3


class TestE2EFailureHandling:
    """End-to-end tests for failure scenarios."""

    @pytest.mark.asyncio
    async def test_task_failure_marks_execution_failed(
        self, e2e_client: AsyncClient, repo_dir: Path
    ):
        """When a task fails, execution status is 'failed'."""
        client = e2e_client
        mock_runner = E2EMockRunner(results={"1-2": 1})  # Task 1-2 fails

        project_id = await _create_project(client)
        await _register_repo(client, project_id, repo_dir)
        sequence_id = await _create_sequence(client, project_id)
        await _upload_plan(client, sequence_id, FAILING_TASK_PLAN)

        with patch(
            "wave_server.engine.execution_manager.get_runner",
            return_value=mock_runner,
        ):
            execution_id = await _start_execution(client, sequence_id)
            result = await _poll_execution(client, execution_id)

        assert result["status"] == "failed"

        # 1-1 ran, 1-2 ran and failed, 1-3 was skipped
        assert "1-1" in mock_runner.spawned
        assert "1-2" in mock_runner.spawned
        assert "1-3" not in mock_runner.spawned

        # Verify events reflect failure
        r = await client.get(f"/api/v1/executions/{execution_id}/events")
        events = r.json()
        event_types = [e["event_type"] for e in events]
        assert "task_completed" in event_types  # 1-1 completed
        assert "task_failed" in event_types  # 1-2 failed

        # run_completed should indicate failure
        run_completed = next(e for e in events if e["event_type"] == "run_completed")
        payload = json.loads(run_completed["payload"])
        assert payload["passed"] is False

    @pytest.mark.asyncio
    async def test_no_plan_fails_fast(self, e2e_client: AsyncClient, repo_dir: Path):
        """No plan → preflight rejects with 422 before creating an execution record."""
        client = e2e_client

        project_id = await _create_project(client)
        await _register_repo(client, project_id, repo_dir)
        sequence_id = await _create_sequence(client, project_id)
        # Don't upload a plan

        r = await client.post(f"/api/v1/sequences/{sequence_id}/executions", json={})
        assert r.status_code == 422
        assert "No plan" in r.json()["detail"]

        # No execution record should have been created
        execs = await client.get(f"/api/v1/sequences/{sequence_id}/executions")
        assert execs.json() == []

    @pytest.mark.asyncio
    async def test_no_repo_fails_fast(self, e2e_client: AsyncClient):
        """No repo → preflight rejects with 422 before creating an execution record."""
        client = e2e_client

        project_id = await _create_project(client)
        # No repo registered
        sequence_id = await _create_sequence(client, project_id)
        await _upload_plan(client, sequence_id, SIMPLE_PLAN)

        r = await client.post(f"/api/v1/sequences/{sequence_id}/executions", json={})
        assert r.status_code == 422
        assert "repository" in r.json()["detail"].lower()

        # No execution record should have been created
        execs = await client.get(f"/api/v1/sequences/{sequence_id}/executions")
        assert execs.json() == []


class TestE2ECancelAndContinue:
    """End-to-end tests for cancel and continue flows."""

    @pytest.mark.asyncio
    async def test_cancel_then_continue(self, e2e_client: AsyncClient, repo_dir: Path):
        """Cancel a running execution, then continue it."""
        client = e2e_client

        project_id = await _create_project(client)
        await _register_repo(client, project_id, repo_dir)
        sequence_id = await _create_sequence(client, project_id)
        await _upload_plan(client, sequence_id, SIMPLE_PLAN)

        # Start with a slow runner so we can cancel it
        slow_runner = E2EMockRunner(delay_s=5.0)
        with patch(
            "wave_server.engine.execution_manager.get_runner",
            return_value=slow_runner,
        ):
            execution_id = await _start_execution(client, sequence_id)

            # Give it a moment to start
            await asyncio.sleep(0.2)

            # Cancel
            r = await client.post(f"/api/v1/executions/{execution_id}/cancel")
            assert r.status_code == 204

        # Verify cancelled
        r = await client.get(f"/api/v1/executions/{execution_id}")
        assert r.json()["status"] == "cancelled"

        # Continue with fast runner
        fast_runner = E2EMockRunner()
        with patch(
            "wave_server.engine.execution_manager.get_runner",
            return_value=fast_runner,
        ):
            r = await client.post(f"/api/v1/executions/{execution_id}/continue")
            assert r.status_code == 201
            new_execution_id = r.json()["id"]
            assert new_execution_id != execution_id
            assert r.json()["trigger"] == "continuation"

            result = await _poll_execution(client, new_execution_id)

        assert result["status"] == "completed"


class TestE2EContinueSkipsCompleted:
    """End-to-end tests that continuation skips already-completed tasks."""

    @pytest.mark.asyncio
    async def test_continue_skips_completed_tasks(self, e2e_client: AsyncClient, repo_dir: Path):
        """When task 1-2 fails, continue should skip 1-1 and re-run 1-2 + 1-3."""
        client = e2e_client

        project_id = await _create_project(client)
        await _register_repo(client, project_id, repo_dir)
        sequence_id = await _create_sequence(client, project_id)
        await _upload_plan(client, sequence_id, FAILING_TASK_PLAN)

        # First run: task 1-1 succeeds, 1-2 fails, 1-3 skipped
        fail_runner = E2EMockRunner(results={"1-2": 1})
        with patch(
            "wave_server.engine.execution_manager.get_runner",
            return_value=fail_runner,
        ):
            exec_id = await _start_execution(client, sequence_id)
            result = await _poll_execution(client, exec_id)

        assert result["status"] == "failed"

        # Verify task 1-1 completed in the first run
        r = await client.get(f"/api/v1/executions/{exec_id}/tasks")
        tasks = {t["task_id"]: t["status"] for t in r.json()}
        assert tasks["1-1"] == "completed"
        assert tasks["1-2"] == "failed"
        # 1-3 is dependency-skipped at the DAG level (no event emitted)

        # Continue: all tasks succeed now. 1-1 should be skipped (already done).
        success_runner = E2EMockRunner()
        with patch(
            "wave_server.engine.execution_manager.get_runner",
            return_value=success_runner,
        ):
            r = await client.post(f"/api/v1/executions/{exec_id}/continue")
            assert r.status_code == 201
            new_exec_id = r.json()["id"]
            assert r.json()["continued_from"] == exec_id

            result = await _poll_execution(client, new_exec_id)

        assert result["status"] == "completed"

        # The runner should NOT have been asked to run task 1-1
        assert "1-1" not in success_runner.spawned
        # But SHOULD have run 1-2 and 1-3
        assert "1-2" in success_runner.spawned
        assert "1-3" in success_runner.spawned

    @pytest.mark.asyncio
    async def test_continue_multi_wave_skips_completed_wave(
        self, e2e_client: AsyncClient, repo_dir: Path
    ):
        """Wave 1 passes, wave 2 fails → continue skips all wave 1 tasks."""
        client = e2e_client

        project_id = await _create_project(client)
        await _register_repo(client, project_id, repo_dir)
        sequence_id = await _create_sequence(client, project_id)
        await _upload_plan(client, sequence_id, MULTI_WAVE_PLAN)

        # First run: wave 2 task 2-2 fails (wave 1 fully passes)
        fail_runner = E2EMockRunner(results={"2-2": 1})
        with patch(
            "wave_server.engine.execution_manager.get_runner",
            return_value=fail_runner,
        ):
            exec_id = await _start_execution(client, sequence_id)
            result = await _poll_execution(client, exec_id)

        assert result["status"] == "failed"

        # Continue: all succeed now
        success_runner = E2EMockRunner()
        with patch(
            "wave_server.engine.execution_manager.get_runner",
            return_value=success_runner,
        ):
            r = await client.post(f"/api/v1/executions/{exec_id}/continue")
            new_exec_id = r.json()["id"]
            result = await _poll_execution(client, new_exec_id)

        assert result["status"] == "completed"

        # Wave 1 tasks should all be skipped (1-f1, 1-a1, 1-a2, 1-p1, 1-i1)
        for tid in ["1-f1", "1-a1", "1-a2", "1-p1", "1-i1"]:
            assert tid not in success_runner.spawned, f"{tid} should have been skipped"

        # Wave 2 task 2-1 completed in first run → should be skipped
        assert "2-1" not in success_runner.spawned
        # Wave 2 task 2-2 failed → should be re-run
        assert "2-2" in success_runner.spawned

    @pytest.mark.asyncio
    async def test_continue_no_completed_tasks(
        self, e2e_client: AsyncClient, repo_dir: Path
    ):
        """When first task fails immediately, continue re-runs everything."""
        client = e2e_client

        project_id = await _create_project(client)
        await _register_repo(client, project_id, repo_dir)
        sequence_id = await _create_sequence(client, project_id)
        await _upload_plan(client, sequence_id, FAILING_TASK_PLAN)

        # First run: task 1-1 (the very first task) fails
        fail_runner = E2EMockRunner(results={"1-1": 1})
        with patch(
            "wave_server.engine.execution_manager.get_runner",
            return_value=fail_runner,
        ):
            exec_id = await _start_execution(client, sequence_id)
            result = await _poll_execution(client, exec_id)

        assert result["status"] == "failed"

        # Continue: nothing to skip, all tasks re-run
        success_runner = E2EMockRunner()
        with patch(
            "wave_server.engine.execution_manager.get_runner",
            return_value=success_runner,
        ):
            r = await client.post(f"/api/v1/executions/{exec_id}/continue")
            new_exec_id = r.json()["id"]
            result = await _poll_execution(client, new_exec_id)

        assert result["status"] == "completed"
        # All three tasks should have been spawned
        assert "1-1" in success_runner.spawned
        assert "1-2" in success_runner.spawned
        assert "1-3" in success_runner.spawned

    @pytest.mark.asyncio
    async def test_double_continue(self, e2e_client: AsyncClient, repo_dir: Path):
        """Continue a continuation — second continue should skip tasks from both ancestors."""
        client = e2e_client

        project_id = await _create_project(client)
        await _register_repo(client, project_id, repo_dir)
        sequence_id = await _create_sequence(client, project_id)
        await _upload_plan(client, sequence_id, FAILING_TASK_PLAN)

        # Run 1: task 1-1 succeeds, 1-2 fails
        runner1 = E2EMockRunner(results={"1-2": 1})
        with patch(
            "wave_server.engine.execution_manager.get_runner",
            return_value=runner1,
        ):
            exec1_id = await _start_execution(client, sequence_id)
            await _poll_execution(client, exec1_id)

        # Continue 1: 1-2 succeeds now, but 1-3 fails
        runner2 = E2EMockRunner(results={"1-3": 1})
        with patch(
            "wave_server.engine.execution_manager.get_runner",
            return_value=runner2,
        ):
            r = await client.post(f"/api/v1/executions/{exec1_id}/continue")
            exec2_id = r.json()["id"]
            result = await _poll_execution(client, exec2_id)

        assert result["status"] == "failed"
        # 1-1 was skipped (from exec1), 1-2 ran and passed, 1-3 ran and failed
        assert "1-1" not in runner2.spawned
        assert "1-2" in runner2.spawned
        assert "1-3" in runner2.spawned

        # Continue 2: continue the continuation
        runner3 = E2EMockRunner()
        with patch(
            "wave_server.engine.execution_manager.get_runner",
            return_value=runner3,
        ):
            r = await client.post(f"/api/v1/executions/{exec2_id}/continue")
            exec3_id = r.json()["id"]
            result = await _poll_execution(client, exec3_id)

        assert result["status"] == "completed"
        # 1-1 and 1-2 both completed in exec2 → both skipped
        assert "1-1" not in runner3.spawned
        assert "1-2" not in runner3.spawned
        # 1-3 failed in exec2 → re-run
        assert "1-3" in runner3.spawned

    @pytest.mark.asyncio
    async def test_continue_reuses_work_branch(
        self, e2e_client: AsyncClient, repo_dir: Path
    ):
        """Continuation should reuse the original execution's work branch."""
        client = e2e_client

        project_id = await _create_project(client)
        await _register_repo(client, project_id, repo_dir)
        sequence_id = await _create_sequence(client, project_id)
        await _upload_plan(client, sequence_id, FAILING_TASK_PLAN)

        # First run: fails
        fail_runner = E2EMockRunner(results={"1-2": 1})
        with patch(
            "wave_server.engine.execution_manager.get_runner",
            return_value=fail_runner,
        ):
            exec_id = await _start_execution(client, sequence_id)
            result = await _poll_execution(client, exec_id)

        original_branch = result.get("work_branch")

        # Continue
        success_runner = E2EMockRunner()
        with patch(
            "wave_server.engine.execution_manager.get_runner",
            return_value=success_runner,
        ):
            r = await client.post(f"/api/v1/executions/{exec_id}/continue")
            new_exec_id = r.json()["id"]
            # Work branch should be copied from original
            assert r.json()["work_branch"] == original_branch
            result = await _poll_execution(client, new_exec_id)

        assert result["status"] == "completed"
        assert result["work_branch"] == original_branch


class TestE2EContextFiles:
    """End-to-end test that context files are injected into prompts."""

    @pytest.mark.asyncio
    async def test_context_files_in_prompts(self, e2e_client: AsyncClient, repo_dir: Path):
        """Verify project context files appear in task prompts."""
        client = e2e_client
        mock_runner = E2EMockRunner()

        # Create architecture doc in repo
        (repo_dir / "ARCHITECTURE.md").write_text("# Architecture\nMicroservices pattern")

        project_id = await _create_project(client)
        await _register_repo(client, project_id, repo_dir)

        # Add context file
        r = await client.post(
            f"/api/v1/projects/{project_id}/context-files",
            json={"path": "ARCHITECTURE.md", "description": "Architecture overview"},
        )
        assert r.status_code == 201

        sequence_id = await _create_sequence(client, project_id)
        await _upload_plan(client, sequence_id, SIMPLE_PLAN)

        with patch(
            "wave_server.engine.execution_manager.get_runner",
            return_value=mock_runner,
        ):
            execution_id = await _start_execution(client, sequence_id)
            await _poll_execution(client, execution_id)

        # Verify context appears in prompts
        for task_id in mock_runner.prompts:
            assert "Microservices pattern" in mock_runner.prompts[task_id]
            assert "Architecture overview" in mock_runner.prompts[task_id]


class TestE2EArtifacts:
    """End-to-end tests for storage artifact creation."""

    @pytest.mark.asyncio
    async def test_all_artifacts_created(self, e2e_client: AsyncClient, repo_dir: Path, tmp_path: Path):
        """Verify that output, transcript, task-log, and execution log are created."""
        client = e2e_client
        mock_runner = E2EMockRunner()

        project_id = await _create_project(client)
        await _register_repo(client, project_id, repo_dir)
        sequence_id = await _create_sequence(client, project_id)
        await _upload_plan(client, sequence_id, SIMPLE_PLAN)

        with patch(
            "wave_server.engine.execution_manager.get_runner",
            return_value=mock_runner,
        ):
            execution_id = await _start_execution(client, sequence_id)
            await _poll_execution(client, execution_id)

        # Output files
        for task_id in ["1-1", "1-2", "1-3"]:
            r = await client.get(f"/api/v1/executions/{execution_id}/output/{task_id}")
            assert r.status_code == 200, f"Missing output for {task_id}"

        # Transcript files
        for task_id in ["1-1", "1-2", "1-3"]:
            r = await client.get(f"/api/v1/executions/{execution_id}/transcript/{task_id}")
            assert r.status_code == 200, f"Missing transcript for {task_id}"

        # Task logs
        r = await client.get(f"/api/v1/executions/{execution_id}/task-logs")
        assert r.status_code == 200
        task_logs = r.json()
        assert len(task_logs) >= 3  # At least one per task

        # Execution log
        r = await client.get(f"/api/v1/executions/{execution_id}/log")
        assert r.status_code == 200
        log_content = r.text
        assert "Wave 1" in log_content or "wave" in log_content.lower()

    @pytest.mark.asyncio
    async def test_task_summary_enrichment(self, e2e_client: AsyncClient, repo_dir: Path):
        """Task summary includes has_output, has_transcript, has_task_log flags."""
        client = e2e_client
        mock_runner = E2EMockRunner()

        project_id = await _create_project(client)
        await _register_repo(client, project_id, repo_dir)
        sequence_id = await _create_sequence(client, project_id)
        await _upload_plan(client, sequence_id, SIMPLE_PLAN)

        with patch(
            "wave_server.engine.execution_manager.get_runner",
            return_value=mock_runner,
        ):
            execution_id = await _start_execution(client, sequence_id)
            await _poll_execution(client, execution_id)

        r = await client.get(f"/api/v1/executions/{execution_id}/tasks")
        assert r.status_code == 200
        tasks = r.json()

        for task in tasks:
            assert task["has_output"] is True
            assert task["has_transcript"] is True


class TestE2EEdgeCases:
    """Edge cases and robustness tests."""

    @pytest.mark.asyncio
    async def test_empty_plan_fails_gracefully(self, e2e_client: AsyncClient, repo_dir: Path):
        """A plan without a format version tag should be rejected at preflight."""
        client = e2e_client

        empty_plan = "# Implementation Plan\n\n## Goal\nNothing to do.\n"

        project_id = await _create_project(client)
        await _register_repo(client, project_id, repo_dir)
        sequence_id = await _create_sequence(client, project_id)
        await _upload_plan(client, sequence_id, empty_plan)

        # Starting execution should fail with 422 — plan has no format version
        r = await client.post(f"/api/v1/sequences/{sequence_id}/executions", json={})
        assert r.status_code == 422
        assert "format version" in r.json()["detail"]

    @pytest.mark.asyncio
    async def test_concurrent_executions_same_sequence(
        self, e2e_client: AsyncClient, repo_dir: Path
    ):
        """Two executions of the same sequence can run without interference."""
        client = e2e_client

        project_id = await _create_project(client)
        await _register_repo(client, project_id, repo_dir)
        sequence_id = await _create_sequence(client, project_id)
        await _upload_plan(client, sequence_id, SIMPLE_PLAN)

        mock_runner = E2EMockRunner(delay_s=0.01)

        # Keep the mock runner alive for both executions — the patch must
        # span the entire poll period since background tasks use it.
        with patch(
            "wave_server.engine.execution_manager.get_runner",
            return_value=mock_runner,
        ):
            exec_id_1 = await _start_execution(client, sequence_id)
            exec_id_2 = await _start_execution(client, sequence_id)

            # Wait for both
            result1 = await _poll_execution(client, exec_id_1)
            result2 = await _poll_execution(client, exec_id_2)

        assert result1["status"] == "completed"
        assert result2["status"] == "completed"

    @pytest.mark.asyncio
    async def test_wave1_failure_prevents_wave2(self, e2e_client: AsyncClient, repo_dir: Path):
        """If wave 1 fails, wave 2 tasks are never spawned."""
        client = e2e_client
        # Fail a task in wave 1 of the multi-wave plan
        mock_runner = E2EMockRunner(results={"1-a2": 1})

        project_id = await _create_project(client)
        await _register_repo(client, project_id, repo_dir)
        sequence_id = await _create_sequence(client, project_id)
        await _upload_plan(client, sequence_id, MULTI_WAVE_PLAN)

        with patch(
            "wave_server.engine.execution_manager.get_runner",
            return_value=mock_runner,
        ):
            execution_id = await _start_execution(client, sequence_id)
            result = await _poll_execution(client, execution_id)

        assert result["status"] == "failed"

        # Wave 2 tasks should never have been spawned
        assert "2-1" not in mock_runner.spawned
        assert "2-2" not in mock_runner.spawned


# ── Rate Limit Simulation (Pi Runtime) ─────────────────────────

from pi_test_helpers import RateLimitPiMockRunner

assert isinstance(RateLimitPiMockRunner(set()), AgentRunner)


class TestE2ERateLimitDetection:
    """E2E: rate-limited pi tasks are correctly marked as failed through
    the full execution pipeline (API → execution_manager → wave_executor)."""

    @pytest.mark.asyncio
    async def test_rate_limited_task_fails_execution(
        self, e2e_client: AsyncClient, repo_dir: Path
    ):
        """A rate-limited task should cause the execution to fail,
        emit task_failed event, and mark dependent tasks as skipped."""
        client = e2e_client
        # Task 1-2 gets rate limited → 1-3 (depends on 1-2) should be skipped
        mock_runner = RateLimitPiMockRunner(rate_limited_task_ids={"1-2"})

        project_id = await _create_project(client)
        await _register_repo(client, project_id, repo_dir)
        sequence_id = await _create_sequence(client, project_id)
        await _upload_plan(client, sequence_id, FAILING_TASK_PLAN)

        with patch(
            "wave_server.engine.execution_manager.get_runner",
            return_value=mock_runner,
        ):
            execution_id = await _start_execution(client, sequence_id)
            result = await _poll_execution(client, execution_id)

        # Execution should be failed
        assert result["status"] == "failed"

        # 1-1 succeeded, 1-2 was rate limited (failed), 1-3 was never spawned
        assert "1-1" in mock_runner.spawned
        assert "1-2" in mock_runner.spawned
        assert "1-3" not in mock_runner.spawned

        # Verify events reflect the rate limit failure
        r = await client.get(f"/api/v1/executions/{execution_id}/events")
        events = r.json()
        event_types = [e["event_type"] for e in events]

        assert "task_completed" in event_types  # 1-1
        assert "task_failed" in event_types      # 1-2 (rate limited)

        # Verify the failed task has exit_code != 0
        failed_events = [
            e for e in events
            if e["event_type"] == "task_failed" and e["task_id"] == "1-2"
        ]
        assert len(failed_events) == 1
        payload = json.loads(failed_events[0]["payload"])
        assert payload["exit_code"] == 1

        # run_completed should indicate failure
        run_completed = next(e for e in events if e["event_type"] == "run_completed")
        payload = json.loads(run_completed["payload"])
        assert payload["passed"] is False

    @pytest.mark.asyncio
    async def test_all_tasks_rate_limited(
        self, e2e_client: AsyncClient, repo_dir: Path
    ):
        """When the first task is rate limited, all dependents are skipped."""
        client = e2e_client
        mock_runner = RateLimitPiMockRunner(rate_limited_task_ids={"1-1"})

        project_id = await _create_project(client)
        await _register_repo(client, project_id, repo_dir)
        sequence_id = await _create_sequence(client, project_id)
        await _upload_plan(client, sequence_id, FAILING_TASK_PLAN)

        with patch(
            "wave_server.engine.execution_manager.get_runner",
            return_value=mock_runner,
        ):
            execution_id = await _start_execution(client, sequence_id)
            result = await _poll_execution(client, execution_id)

        assert result["status"] == "failed"

        # Only 1-1 was spawned (it failed), 1-2 and 1-3 never ran
        assert mock_runner.spawned == ["1-1"]

        # Verify events: 1-1 started and failed, no other tasks started
        r = await client.get(f"/api/v1/executions/{execution_id}/events")
        events = r.json()
        event_types = [e["event_type"] for e in events]

        assert "task_failed" in event_types
        failed_events = [e for e in events if e["event_type"] == "task_failed"]
        assert len(failed_events) == 1
        assert failed_events[0]["task_id"] == "1-1"

        # No task_completed events since the only task that ran was rate limited
        started_task_ids = {e["task_id"] for e in events if e["event_type"] == "task_started"}
        assert started_task_ids == {"1-1"}
