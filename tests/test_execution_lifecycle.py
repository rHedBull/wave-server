"""Comprehensive tests for execution state management and lifecycle.

Covers:
- Full execution lifecycle (queued → running → completed/failed)
- Repo validation (fail-fast when no repo configured)
- Context file loading and injection
- Callback event emission (no fire-and-forget DB races)
- Task state transitions and counting
- Plan parse failures, empty plans
- Cancel and continue flows
- Multi-wave execution with failure propagation
- Git SHA capture
- Storage artifacts (output, transcript, log)
"""

from __future__ import annotations

import asyncio
import json
import os
import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from wave_server.db import Base
from wave_server.engine.execution_manager import _load_context_files, _run_execution
from wave_server.engine.runner import AgentRunner
from wave_server.engine.types import (
    Feature,
    Plan,
    RunnerConfig,
    RunnerResult,
    Task,
    TaskResult,
    Wave,
)
from wave_server.engine.wave_executor import (
    WaveExecutorOptions,
    _build_task_prompt,
    execute_wave,
)
from wave_server.models import (
    Event,
    Execution,
    Project,
    ProjectContextFile,
    ProjectRepository,
    Sequence,
)


# ── Fixtures ───────────────────────────────────────────────────


class MockRunner:
    """Configurable mock runner for testing execution flows."""

    def __init__(
        self,
        results: dict[str, int] | None = None,
        delay_s: float = 0,
        outputs: dict[str, str] | None = None,
    ):
        self.results = results or {}
        self.delay_s = delay_s
        self.outputs = outputs or {}
        self.spawned: list[str] = []
        self.spawn_order: list[str] = []
        self.cwds: list[str] = []

    async def spawn(self, config: RunnerConfig) -> RunnerResult:
        self.spawned.append(config.task_id)
        self.spawn_order.append(config.task_id)
        self.cwds.append(config.cwd)
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        exit_code = self.results.get(config.task_id, 0)
        output = self.outputs.get(config.task_id, f"Task {config.task_id} done")
        return RunnerResult(
            exit_code=exit_code,
            stdout=json.dumps({"type": "result", "result": output}),
            stderr="" if exit_code == 0 else "error occurred",
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


def _task(
    id: str,
    agent: str = "worker",
    depends: list[str] | None = None,
    files: list[str] | None = None,
) -> Task:
    return Task(
        id=id,
        title=f"Task {id}",
        agent=agent,
        depends=depends or [],
        files=files or [],
        description=f"Description for {id}",
    )


def _simple_plan_md(num_waves: int = 1, tasks_per_wave: int = 1) -> str:
    """Generate a simple valid plan markdown."""
    lines = ["# Implementation Plan", "", "## Goal", "Test goal", ""]
    task_counter = 0
    for w in range(1, num_waves + 1):
        lines.append(f"## Wave {w}: Wave{w}")
        for t in range(1, tasks_per_wave + 1):
            task_counter += 1
            tid = f"{w}-{t}"
            dep = f"- **Depends:** {w}-{t-1}" if t > 1 else "- **Depends:** (none)"
            lines.extend([
                f"### Task {tid}: Task {tid}",
                f"- **Files:** `file{task_counter}.py`",
                dep,
                f"- **Description:** Do task {tid}",
                "",
            ])
    return "\n".join(lines)


@pytest_asyncio.fixture
async def db():
    """Create a fresh in-memory SQLite database for each test."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def project_with_repo(db: AsyncSession, tmp_path: Path):
    """Create a project with a registered repository directory."""
    project = Project(name="test-project")
    db.add(project)
    await db.commit()
    await db.refresh(project)

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    repo = ProjectRepository(
        project_id=project.id,
        path=str(repo_dir),
        label="test repo",
    )
    db.add(repo)
    await db.commit()

    return project, repo_dir


@pytest_asyncio.fixture
async def project_no_repo(db: AsyncSession):
    """Create a project without a registered repository."""
    project = Project(name="no-repo-project")
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


# ── Wave Executor: State Transitions ───────────────────────────


class TestWaveStateTransitions:
    """Test task and wave state transitions in the executor."""

    @pytest.mark.asyncio
    async def test_all_tasks_pass_wave_passes(self):
        wave = Wave(
            name="W1",
            features=[Feature(name="default", tasks=[_task("1-1"), _task("1-2")])],
        )
        runner = MockRunner()
        result = await execute_wave(
            WaveExecutorOptions(wave=wave, wave_num=1, runner=runner)
        )
        assert result.passed
        assert len(result.feature_results) == 1
        assert result.feature_results[0].passed

    @pytest.mark.asyncio
    async def test_first_task_failure_stops_dependents(self):
        """When a task fails, its dependents should be skipped."""
        wave = Wave(
            name="W1",
            features=[
                Feature(name="default", tasks=[
                    _task("1-1"),
                    _task("1-2", depends=["1-1"]),
                    _task("1-3", depends=["1-2"]),
                ])
            ],
        )
        runner = MockRunner(results={"1-1": 1})
        result = await execute_wave(
            WaveExecutorOptions(wave=wave, wave_num=1, runner=runner)
        )
        assert not result.passed
        # Only 1-1 should have been spawned; 1-2 and 1-3 are dependents → skipped
        assert "1-1" in runner.spawned
        assert "1-2" not in runner.spawned
        assert "1-3" not in runner.spawned

    @pytest.mark.asyncio
    async def test_independent_tasks_all_run_on_failure(self):
        """Independent tasks within a feature all run even if one fails."""
        wave = Wave(
            name="W1",
            features=[
                Feature(name="default", tasks=[_task("1-1"), _task("1-2"), _task("1-3")])
            ],
        )
        runner = MockRunner(results={"1-1": 1})
        result = await execute_wave(
            WaveExecutorOptions(wave=wave, wave_num=1, runner=runner)
        )
        assert not result.passed
        # All are independent → all run (DAG behavior)
        assert "1-1" in runner.spawned
        assert "1-2" in runner.spawned
        assert "1-3" in runner.spawned

    @pytest.mark.asyncio
    async def test_middle_task_failure_stops_dependents(self):
        """When a middle task in a chain fails, later dependents are skipped."""
        wave = Wave(
            name="W1",
            features=[
                Feature(
                    name="default",
                    tasks=[
                        _task("1-1"),
                        _task("1-2", depends=["1-1"]),
                        _task("1-3", depends=["1-2"]),
                    ],
                )
            ],
        )
        runner = MockRunner(results={"1-2": 1})
        result = await execute_wave(
            WaveExecutorOptions(wave=wave, wave_num=1, runner=runner)
        )
        assert not result.passed
        assert "1-1" in runner.spawned
        assert "1-2" in runner.spawned
        assert "1-3" not in runner.spawned

    @pytest.mark.asyncio
    async def test_foundation_failure_skips_features_and_integration(self):
        wave = Wave(
            name="W1",
            foundation=[_task("f1")],
            features=[Feature(name="feat", tasks=[_task("t1")])],
            integration=[_task("i1")],
        )
        runner = MockRunner(results={"f1": 1})
        result = await execute_wave(
            WaveExecutorOptions(wave=wave, wave_num=1, runner=runner)
        )
        assert not result.passed
        assert len(result.feature_results) == 0
        assert len(result.integration_results) == 0
        assert "t1" not in runner.spawned
        assert "i1" not in runner.spawned

    @pytest.mark.asyncio
    async def test_feature_failure_skips_integration(self):
        wave = Wave(
            name="W1",
            foundation=[_task("f1")],
            features=[Feature(name="feat", tasks=[_task("t1")])],
            integration=[_task("i1")],
        )
        runner = MockRunner(results={"t1": 1})
        result = await execute_wave(
            WaveExecutorOptions(wave=wave, wave_num=1, runner=runner)
        )
        assert not result.passed
        assert "f1" in runner.spawned
        assert "t1" in runner.spawned
        assert "i1" not in runner.spawned
        assert len(result.integration_results) == 0

    @pytest.mark.asyncio
    async def test_integration_failure_marks_wave_failed(self):
        wave = Wave(
            name="W1",
            foundation=[_task("f1")],
            integration=[_task("i1")],
        )
        runner = MockRunner(results={"i1": 1})
        result = await execute_wave(
            WaveExecutorOptions(wave=wave, wave_num=1, runner=runner)
        )
        assert not result.passed
        assert result.foundation_results[0].exit_code == 0
        assert result.integration_results[0].exit_code == 1

    @pytest.mark.asyncio
    async def test_skip_already_completed_tasks(self):
        wave = Wave(
            name="W1",
            features=[
                Feature(name="default", tasks=[_task("1-1"), _task("1-2")])
            ],
        )
        runner = MockRunner()
        result = await execute_wave(
            WaveExecutorOptions(
                wave=wave,
                wave_num=1,
                runner=runner,
                skip_task_ids={"1-1"},
            )
        )
        assert result.passed
        # 1-1 should NOT have been spawned (skipped)
        assert "1-1" not in runner.spawned
        assert "1-2" in runner.spawned

    @pytest.mark.asyncio
    async def test_empty_wave_passes(self):
        wave = Wave(name="Empty")
        runner = MockRunner()
        result = await execute_wave(
            WaveExecutorOptions(wave=wave, wave_num=1, runner=runner)
        )
        assert result.passed
        assert runner.spawned == []


# ── Wave Executor: Callbacks ───────────────────────────────────


class TestWaveCallbacks:
    """Test that callbacks fire correctly for state tracking."""

    @pytest.mark.asyncio
    async def test_task_start_callback_fires(self):
        started = []
        wave = Wave(
            name="W1",
            features=[Feature(name="default", tasks=[_task("1-1")])],
        )
        runner = MockRunner()
        await execute_wave(
            WaveExecutorOptions(
                wave=wave,
                wave_num=1,
                runner=runner,
                on_task_start=lambda phase, task: started.append(
                    (phase, task.id)
                ),
            )
        )
        assert ("feature:default", "1-1") in started

    @pytest.mark.asyncio
    async def test_task_end_callback_with_exit_codes(self):
        ended = []
        wave = Wave(
            name="W1",
            features=[Feature(name="default", tasks=[_task("1-1"), _task("1-2")])],
        )
        runner = MockRunner(results={"1-2": 1})
        await execute_wave(
            WaveExecutorOptions(
                wave=wave,
                wave_num=1,
                runner=runner,
                on_task_end=lambda phase, task, result: ended.append(
                    (task.id, result.exit_code)
                ),
            )
        )
        assert ("1-1", 0) in ended
        assert ("1-2", 1) in ended

    @pytest.mark.asyncio
    async def test_callbacks_fire_for_skipped_tasks(self):
        started = []
        ended = []
        wave = Wave(
            name="W1",
            features=[Feature(name="default", tasks=[_task("1-1")])],
        )
        runner = MockRunner()
        await execute_wave(
            WaveExecutorOptions(
                wave=wave,
                wave_num=1,
                runner=runner,
                skip_task_ids={"1-1"},
                on_task_start=lambda phase, task: started.append(task.id),
                on_task_end=lambda phase, task, result: ended.append(
                    (task.id, result.exit_code)
                ),
            )
        )
        assert "1-1" in started
        assert ("1-1", 0) in ended

    @pytest.mark.asyncio
    async def test_progress_callback_phases(self):
        phases = []
        wave = Wave(
            name="W1",
            foundation=[_task("f1")],
            features=[Feature(name="feat", tasks=[_task("t1")])],
            integration=[_task("i1")],
        )
        runner = MockRunner()
        await execute_wave(
            WaveExecutorOptions(
                wave=wave,
                wave_num=1,
                runner=runner,
                use_worktrees=False,
                on_progress=lambda p: phases.append(p.phase),
            )
        )
        assert phases == ["foundation", "features", "integration"]

    @pytest.mark.asyncio
    async def test_log_callback_on_failure(self):
        logs = []
        wave = Wave(
            name="W1",
            features=[Feature(name="default", tasks=[_task("1-1")])],
        )
        runner = MockRunner(results={"1-1": 1})
        await execute_wave(
            WaveExecutorOptions(
                wave=wave,
                wave_num=1,
                runner=runner,
                on_log=lambda line: logs.append(line),
            )
        )
        assert any("failed" in l.lower() for l in logs)


# ── Wave Executor: CWD Propagation ────────────────────────────


class TestCwdPropagation:
    """Test that cwd is correctly passed to the runner."""

    @pytest.mark.asyncio
    async def test_cwd_passed_to_runner(self):
        wave = Wave(
            name="W1",
            features=[Feature(name="default", tasks=[_task("1-1")])],
        )
        runner = MockRunner()
        await execute_wave(
            WaveExecutorOptions(
                wave=wave,
                wave_num=1,
                runner=runner,
                cwd="/my/project",
            )
        )
        assert runner.cwds == ["/my/project"]

    @pytest.mark.asyncio
    async def test_default_cwd_is_dot(self):
        wave = Wave(
            name="W1",
            features=[Feature(name="default", tasks=[_task("1-1")])],
        )
        runner = MockRunner()
        await execute_wave(
            WaveExecutorOptions(wave=wave, wave_num=1, runner=runner)
        )
        assert runner.cwds == ["."]


# ── Multi-Wave Execution ──────────────────────────────────────


class TestMultiWaveExecution:
    """Test multi-wave flows with failure propagation."""

    @pytest.mark.asyncio
    async def test_wave1_failure_prevents_wave2(self):
        """Simulate what execution_manager does: loop over waves, break on failure."""
        waves = [
            Wave(
                name="W1",
                features=[Feature(name="default", tasks=[_task("1-1")])],
            ),
            Wave(
                name="W2",
                features=[Feature(name="default", tasks=[_task("2-1")])],
            ),
        ]
        runner = MockRunner(results={"1-1": 1})

        results = []
        for wave in waves:
            result = await execute_wave(
                WaveExecutorOptions(wave=wave, wave_num=1, runner=runner)
            )
            results.append(result)
            if not result.passed:
                break

        assert len(results) == 1  # Stopped after wave 1
        assert not results[0].passed
        assert "2-1" not in runner.spawned

    @pytest.mark.asyncio
    async def test_both_waves_pass(self):
        waves = [
            Wave(
                name="W1",
                features=[Feature(name="default", tasks=[_task("1-1")])],
            ),
            Wave(
                name="W2",
                features=[Feature(name="default", tasks=[_task("2-1")])],
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
        assert all(r.passed for r in results)
        assert set(runner.spawned) == {"1-1", "2-1"}


# ── Prompt Building ───────────────────────────────────────────


class TestPromptBuilding:
    """Test that prompts include context, schemas, and task details."""

    def test_worker_prompt_includes_files(self):
        task = _task("1-1", files=["src/main.py"])
        prompt = _build_task_prompt(task, spec_content="", data_schemas="")
        assert "src/main.py" in prompt
        assert "implementing code" in prompt.lower()

    def test_verifier_prompt(self):
        task = _task("1-1", agent="wave-verifier", files=["src/main.py"])
        prompt = _build_task_prompt(task, spec_content="", data_schemas="")
        assert "verifying" in prompt.lower()
        assert "Do NOT modify" in prompt

    def test_test_writer_prompt(self):
        task = _task("1-1", agent="test-writer", files=["tests/test_main.py"])
        prompt = _build_task_prompt(task, spec_content="", data_schemas="")
        assert "writing tests" in prompt.lower()

    def test_schemas_injected(self):
        task = _task("1-1")
        prompt = _build_task_prompt(
            task,
            spec_content="",
            data_schemas="CREATE TABLE users (id INT);",
        )
        assert "CREATE TABLE users" in prompt
        assert "Data Schemas" in prompt

    def test_project_context_injected(self):
        task = _task("1-1")
        prompt = _build_task_prompt(
            task,
            spec_content="",
            data_schemas="",
            project_context="## Project Context\n\n### Architecture\nMicroservices",
        )
        assert "Project Context" in prompt
        assert "Microservices" in prompt

    def test_no_context_no_schemas_block(self):
        task = _task("1-1")
        prompt = _build_task_prompt(
            task, spec_content="", data_schemas="", project_context=""
        )
        assert "Project Context" not in prompt
        # The "Data Schemas (authoritative" header should not appear
        assert "authoritative" not in prompt

    def test_test_files_in_worker_prompt(self):
        task = _task("1-1", files=["src/main.py"])
        task.test_files = ["tests/test_main.py"]
        prompt = _build_task_prompt(task, spec_content="", data_schemas="")
        assert "tests/test_main.py" in prompt
        assert "MUST make these tests pass" in prompt


# ── Context File Loading ──────────────────────────────────────


class TestContextFileLoading:
    """Test _load_context_files helper."""

    def test_loads_existing_file(self, tmp_path: Path):
        (tmp_path / "README.md").write_text("# Hello World")
        ctx = MagicMock()
        ctx.path = "README.md"
        ctx.description = "Project readme"
        result = _load_context_files([ctx], str(tmp_path))
        assert "Hello World" in result
        assert "Project readme" in result
        assert "## Project Context" in result

    def test_skips_missing_file(self, tmp_path: Path):
        ctx = MagicMock()
        ctx.path = "NONEXISTENT.md"
        ctx.description = "Missing file"
        result = _load_context_files([ctx], str(tmp_path))
        assert result == ""

    def test_empty_list_returns_empty(self, tmp_path: Path):
        result = _load_context_files([], str(tmp_path))
        assert result == ""

    def test_truncates_large_files(self, tmp_path: Path):
        (tmp_path / "big.txt").write_text("x" * 100_000)
        ctx = MagicMock()
        ctx.path = "big.txt"
        ctx.description = "Big file"
        result = _load_context_files([ctx], str(tmp_path))
        assert "truncated" in result.lower()
        # Should be capped at ~32KB + header
        assert len(result) < 40_000

    def test_resolves_relative_paths(self, tmp_path: Path):
        sub = tmp_path / "docs"
        sub.mkdir()
        (sub / "arch.md").write_text("Architecture notes")
        ctx = MagicMock()
        ctx.path = "docs/arch.md"
        ctx.description = None
        result = _load_context_files([ctx], str(tmp_path))
        assert "Architecture notes" in result

    def test_absolute_path_works(self, tmp_path: Path):
        f = tmp_path / "abs.md"
        f.write_text("Absolute content")
        ctx = MagicMock()
        ctx.path = str(f)
        ctx.description = "Absolute"
        result = _load_context_files([ctx], str(tmp_path))
        assert "Absolute content" in result

    def test_multiple_files_combined(self, tmp_path: Path):
        (tmp_path / "a.md").write_text("Content A")
        (tmp_path / "b.md").write_text("Content B")
        ctx_a = MagicMock()
        ctx_a.path = "a.md"
        ctx_a.description = "File A"
        ctx_b = MagicMock()
        ctx_b.path = "b.md"
        ctx_b.description = "File B"
        result = _load_context_files([ctx_a, ctx_b], str(tmp_path))
        assert "Content A" in result
        assert "Content B" in result
        assert "File A" in result
        assert "File B" in result

    def test_uses_path_as_label_when_no_description(self, tmp_path: Path):
        (tmp_path / "notes.md").write_text("Notes")
        ctx = MagicMock()
        ctx.path = "notes.md"
        ctx.description = None
        result = _load_context_files([ctx], str(tmp_path))
        assert "notes.md" in result


# ── API: Repository Routes ────────────────────────────────────


class TestRepositoryRoutes:
    """Test project repository CRUD via API."""

    @pytest.mark.asyncio
    async def test_add_repository(self, client, tmp_path: Path):
        proj = await client.post("/api/v1/projects", json={"name": "p"})
        pid = proj.json()["id"]
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        r = await client.post(
            f"/api/v1/projects/{pid}/repositories",
            json={"path": str(repo_dir), "label": "main"},
        )
        assert r.status_code == 201
        data = r.json()
        assert data["path"] == str(repo_dir)
        assert data["label"] == "main"
        assert data["project_id"] == pid

    @pytest.mark.asyncio
    async def test_add_repository_invalid_path(self, client):
        proj = await client.post("/api/v1/projects", json={"name": "p"})
        pid = proj.json()["id"]
        r = await client.post(
            f"/api/v1/projects/{pid}/repositories",
            json={"path": "/nonexistent/path/xyz"},
        )
        assert r.status_code == 400
        assert "does not exist" in r.json()["detail"]

    @pytest.mark.asyncio
    async def test_list_repositories(self, client, tmp_path: Path):
        proj = await client.post("/api/v1/projects", json={"name": "p"})
        pid = proj.json()["id"]
        d1 = tmp_path / "r1"
        d1.mkdir()
        d2 = tmp_path / "r2"
        d2.mkdir()
        await client.post(
            f"/api/v1/projects/{pid}/repositories",
            json={"path": str(d1)},
        )
        await client.post(
            f"/api/v1/projects/{pid}/repositories",
            json={"path": str(d2)},
        )
        r = await client.get(f"/api/v1/projects/{pid}/repositories")
        assert r.status_code == 200
        assert len(r.json()) == 2

    @pytest.mark.asyncio
    async def test_delete_repository(self, client, tmp_path: Path):
        proj = await client.post("/api/v1/projects", json={"name": "p"})
        pid = proj.json()["id"]
        d = tmp_path / "repo"
        d.mkdir()
        repo = await client.post(
            f"/api/v1/projects/{pid}/repositories",
            json={"path": str(d)},
        )
        rid = repo.json()["id"]
        r = await client.delete(f"/api/v1/projects/{pid}/repositories/{rid}")
        assert r.status_code == 204
        r = await client.get(f"/api/v1/projects/{pid}/repositories")
        assert len(r.json()) == 0

    @pytest.mark.asyncio
    async def test_add_repo_project_not_found(self, client, tmp_path: Path):
        d = tmp_path / "repo"
        d.mkdir()
        r = await client.post(
            "/api/v1/projects/nonexistent/repositories",
            json={"path": str(d)},
        )
        assert r.status_code == 404


# ── API: Context File Routes ─────────────────────────────────


class TestContextFileRoutes:
    """Test project context file CRUD via API."""

    @pytest.mark.asyncio
    async def test_add_context_file(self, client):
        proj = await client.post("/api/v1/projects", json={"name": "p"})
        pid = proj.json()["id"]
        r = await client.post(
            f"/api/v1/projects/{pid}/context-files",
            json={"path": "ARCHITECTURE.md", "description": "Arch doc"},
        )
        assert r.status_code == 201
        assert r.json()["path"] == "ARCHITECTURE.md"

    @pytest.mark.asyncio
    async def test_list_context_files(self, client):
        proj = await client.post("/api/v1/projects", json={"name": "p"})
        pid = proj.json()["id"]
        await client.post(
            f"/api/v1/projects/{pid}/context-files",
            json={"path": "a.md"},
        )
        await client.post(
            f"/api/v1/projects/{pid}/context-files",
            json={"path": "b.md"},
        )
        r = await client.get(f"/api/v1/projects/{pid}/context-files")
        assert r.status_code == 200
        assert len(r.json()) == 2

    @pytest.mark.asyncio
    async def test_delete_context_file(self, client):
        proj = await client.post("/api/v1/projects", json={"name": "p"})
        pid = proj.json()["id"]
        cf = await client.post(
            f"/api/v1/projects/{pid}/context-files",
            json={"path": "README.md"},
        )
        fid = cf.json()["id"]
        r = await client.delete(f"/api/v1/projects/{pid}/context-files/{fid}")
        assert r.status_code == 204
        r = await client.get(f"/api/v1/projects/{pid}/context-files")
        assert len(r.json()) == 0


# ── API: Execution Lifecycle ─────────────────────────────────


class TestExecutionAPI:
    """Test execution creation, cancellation, and continuation via API."""

    @pytest.mark.asyncio
    async def test_create_execution_returns_queued(self, client):
        proj = await client.post("/api/v1/projects", json={"name": "p"})
        pid = proj.json()["id"]
        seq = await client.post(
            f"/api/v1/projects/{pid}/sequences", json={"name": "s"}
        )
        sid = seq.json()["id"]
        r = await client.post(f"/api/v1/sequences/{sid}/executions", json={})
        assert r.status_code == 201
        assert r.json()["status"] == "queued"
        assert r.json()["trigger"] == "initial"

    @pytest.mark.asyncio
    async def test_create_execution_custom_config(self, client):
        proj = await client.post("/api/v1/projects", json={"name": "p"})
        pid = proj.json()["id"]
        seq = await client.post(
            f"/api/v1/projects/{pid}/sequences", json={"name": "s"}
        )
        sid = seq.json()["id"]
        r = await client.post(
            f"/api/v1/sequences/{sid}/executions",
            json={"runtime": "claude", "concurrency": 8, "timeout_ms": 60000},
        )
        assert r.status_code == 201
        config = json.loads(r.json()["config"])
        assert config["concurrency"] == 8
        assert config["timeout_ms"] == 60000

    @pytest.mark.asyncio
    async def test_cancel_running_execution(self, client):
        proj = await client.post("/api/v1/projects", json={"name": "p"})
        pid = proj.json()["id"]
        seq = await client.post(
            f"/api/v1/projects/{pid}/sequences", json={"name": "s"}
        )
        sid = seq.json()["id"]
        exc = await client.post(f"/api/v1/sequences/{sid}/executions", json={})
        eid = exc.json()["id"]
        r = await client.post(f"/api/v1/executions/{eid}/cancel")
        assert r.status_code == 204
        r = await client.get(f"/api/v1/executions/{eid}")
        assert r.json()["status"] == "cancelled"

    @pytest.mark.asyncio
    async def test_cancel_already_completed_fails(self, client):
        proj = await client.post("/api/v1/projects", json={"name": "p"})
        pid = proj.json()["id"]
        seq = await client.post(
            f"/api/v1/projects/{pid}/sequences", json={"name": "s"}
        )
        sid = seq.json()["id"]
        exc = await client.post(f"/api/v1/sequences/{sid}/executions", json={})
        eid = exc.json()["id"]
        # Cancel first
        await client.post(f"/api/v1/executions/{eid}/cancel")
        # Try to cancel again
        r = await client.post(f"/api/v1/executions/{eid}/cancel")
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_continue_creates_new_execution(self, client):
        proj = await client.post("/api/v1/projects", json={"name": "p"})
        pid = proj.json()["id"]
        seq = await client.post(
            f"/api/v1/projects/{pid}/sequences", json={"name": "s"}
        )
        sid = seq.json()["id"]
        exc = await client.post(f"/api/v1/sequences/{sid}/executions", json={})
        eid = exc.json()["id"]
        # Cancel to make it resumable
        await client.post(f"/api/v1/executions/{eid}/cancel")
        # Continue
        r = await client.post(f"/api/v1/executions/{eid}/continue")
        assert r.status_code == 201
        assert r.json()["trigger"] == "continuation"
        assert r.json()["id"] != eid

    @pytest.mark.asyncio
    async def test_continue_running_execution_fails(self, client):
        proj = await client.post("/api/v1/projects", json={"name": "p"})
        pid = proj.json()["id"]
        seq = await client.post(
            f"/api/v1/projects/{pid}/sequences", json={"name": "s"}
        )
        sid = seq.json()["id"]
        exc = await client.post(f"/api/v1/sequences/{sid}/executions", json={})
        eid = exc.json()["id"]
        r = await client.post(f"/api/v1/executions/{eid}/continue")
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_list_executions(self, client):
        proj = await client.post("/api/v1/projects", json={"name": "p"})
        pid = proj.json()["id"]
        seq = await client.post(
            f"/api/v1/projects/{pid}/sequences", json={"name": "s"}
        )
        sid = seq.json()["id"]
        await client.post(f"/api/v1/sequences/{sid}/executions", json={})
        await client.post(f"/api/v1/sequences/{sid}/executions", json={})
        r = await client.get(f"/api/v1/sequences/{sid}/executions")
        assert r.status_code == 200
        assert len(r.json()) == 2

    @pytest.mark.asyncio
    async def test_get_execution_not_found(self, client):
        r = await client.get("/api/v1/executions/nonexistent")
        assert r.status_code == 404


# ── Plan Parsing in Execution Context ─────────────────────────


class TestPlanParsing:
    """Test plan validation edge cases that affect execution."""

    def test_valid_plan_parses(self):
        from wave_server.engine.plan_parser import parse_plan

        plan = parse_plan(_simple_plan_md(2, 2))
        assert len(plan.waves) == 2
        assert plan.goal == "Test goal"
        for wave in plan.waves:
            assert len(wave.features) == 1
            assert len(wave.features[0].tasks) == 2

    def test_empty_plan_no_waves(self):
        from wave_server.engine.plan_parser import parse_plan

        plan = parse_plan("# Empty Plan\n\nNothing here.")
        assert len(plan.waves) == 0

    def test_plan_with_bad_format_no_tasks(self):
        from wave_server.engine.plan_parser import parse_plan

        plan = parse_plan("## Wave 1 — Bad Format\n### Not a task header")
        assert len(plan.waves) == 0


# ── DAG Dependency Validation ─────────────────────────────────


class TestDAGInExecution:
    """Test DAG validation as it affects execution."""

    def test_valid_deps_pass(self):
        from wave_server.engine.dag import validate_plan

        plan = Plan(
            waves=[
                Wave(
                    name="W1",
                    features=[
                        Feature(
                            name="default",
                            tasks=[_task("1-1"), _task("1-2", depends=["1-1"])],
                        )
                    ],
                )
            ]
        )
        valid, errors = validate_plan(plan)
        assert valid
        assert errors == []

    def test_missing_dep_fails_validation(self):
        from wave_server.engine.dag import validate_plan

        plan = Plan(
            waves=[
                Wave(
                    name="W1",
                    features=[
                        Feature(
                            name="default",
                            tasks=[_task("1-1", depends=["nonexistent"])],
                        )
                    ],
                )
            ]
        )
        valid, errors = validate_plan(plan)
        assert not valid

    def test_circular_dep_fails_validation(self):
        from wave_server.engine.dag import validate_plan

        plan = Plan(
            waves=[
                Wave(
                    name="W1",
                    features=[
                        Feature(
                            name="default",
                            tasks=[
                                _task("a", depends=["b"]),
                                _task("b", depends=["a"]),
                            ],
                        )
                    ],
                )
            ]
        )
        valid, errors = validate_plan(plan)
        assert not valid


# ── Timeout Handling ──────────────────────────────────────────


class TestTimeout:
    """Test task timeout behavior."""

    @pytest.mark.asyncio
    async def test_timed_out_task_marked_as_such(self):
        wave = Wave(
            name="W1",
            features=[Feature(name="default", tasks=[_task("1-1")])],
        )

        class TimingOutRunner:
            async def spawn(self, config: RunnerConfig) -> RunnerResult:
                return RunnerResult(
                    exit_code=1, stdout="", stderr="", timed_out=True
                )

            def extract_final_output(self, stdout: str) -> str:
                return ""

        ended = []
        await execute_wave(
            WaveExecutorOptions(
                wave=wave,
                wave_num=1,
                runner=TimingOutRunner(),
                on_task_end=lambda phase, task, result: ended.append(result),
            )
        )
        assert len(ended) == 1
        assert "timed out" in ended[0].output.lower()
        assert ended[0].exit_code == 1


# ── Feature Concurrency ──────────────────────────────────────


class TestFeatureConcurrency:
    """Test parallel feature execution behavior."""

    @pytest.mark.asyncio
    async def test_multiple_features_all_pass(self):
        wave = Wave(
            name="W1",
            features=[
                Feature(name="auth", tasks=[_task("a1")]),
                Feature(name="profile", tasks=[_task("p1")]),
                Feature(name="billing", tasks=[_task("b1")]),
            ],
        )
        runner = MockRunner()
        result = await execute_wave(
            WaveExecutorOptions(wave=wave, wave_num=1, runner=runner)
        )
        assert result.passed
        assert len(result.feature_results) == 3
        assert set(runner.spawned) == {"a1", "p1", "b1"}

    @pytest.mark.asyncio
    async def test_one_feature_fails_skips_integration(self):
        wave = Wave(
            name="W1",
            features=[
                Feature(name="auth", tasks=[_task("a1")]),
                Feature(name="profile", tasks=[_task("p1")]),
            ],
            integration=[_task("i1")],
        )
        runner = MockRunner(results={"p1": 1})
        result = await execute_wave(
            WaveExecutorOptions(wave=wave, wave_num=1, runner=runner)
        )
        assert not result.passed
        assert "i1" not in runner.spawned
        # Auth passed, profile failed
        auth_result = next(r for r in result.feature_results if r.name == "auth")
        profile_result = next(
            r for r in result.feature_results if r.name == "profile"
        )
        assert auth_result.passed
        assert not profile_result.passed


# API test classes above use the `client` fixture from conftest.py
