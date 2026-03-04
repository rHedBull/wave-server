"""Integration tests for worktree-isolated execution.

Tests the full flow: wave executor → feature executor → sub-worktrees,
using real git repos and mock runners.
"""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from wave_server.engine.feature_executor import execute_feature
from wave_server.engine.git_worktree import (
    cleanup_worktrees,
    create_feature_worktree,
    get_current_branch,
)
from wave_server.engine.types import (
    Feature,
    FeatureWorktree,
    RunnerConfig,
    RunnerResult,
    Task,
    Wave,
)
from wave_server.engine.wave_executor import WaveExecutorOptions, execute_wave


# ── Helpers ────────────────────────────────────────────────────


def _git(args: str, cwd: str) -> str:
    return subprocess.run(
        ["git"] + args.split(),
        cwd=cwd, capture_output=True, text=True, check=True,
    ).stdout.strip()


def _init_repo(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    _git("init", path)
    _git("config user.email test@test.com", path)
    _git("config user.name Test", path)
    with open(os.path.join(path, "README.md"), "w") as f:
        f.write("# Test\n")
    _git("add -A", path)
    _git("commit -m initial", path)
    return path


def _task(
    id: str, depends: list[str] | None = None, files: list[str] | None = None
) -> Task:
    return Task(
        id=id,
        title=f"Task {id}",
        depends=depends or [],
        files=files or [f"{id}.py"],
    )


class MockRunner:
    """Mock runner that tracks spawns, cwds, and writes files to prove isolation."""

    def __init__(
        self,
        results: dict[str, int] | None = None,
        write_files: bool = False,
    ):
        self.results = results or {}
        self.write_files = write_files
        self.spawned: list[str] = []
        self.cwds: list[str] = []

    async def spawn(self, config: RunnerConfig) -> RunnerResult:
        self.spawned.append(config.task_id)
        self.cwds.append(config.cwd)

        # Simulate agent writing a file in the task's cwd
        if self.write_files:
            filepath = os.path.join(config.cwd, f"{config.task_id}.py")
            with open(filepath, "w") as f:
                f.write(f"# Code from task {config.task_id}\n")

        exit_code = self.results.get(config.task_id, 0)
        return RunnerResult(
            exit_code=exit_code,
            stdout=json.dumps({"type": "result", "result": f"Done {config.task_id}"}),
            stderr="" if exit_code == 0 else "error",
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


# ═══════════════════════════════════════════════════════════════
# FEATURE EXECUTOR WITH WORKTREE ISOLATION
# ═══════════════════════════════════════════════════════════════


class TestFeatureExecutorWithWorktree:
    """Test execute_feature with a real feature worktree."""

    @pytest.mark.asyncio
    async def test_tasks_run_in_worktree_dir(self, tmp_path):
        """When a feature worktree is provided, tasks should run in it."""
        repo = _init_repo(str(tmp_path / "repo"))
        wt = await create_feature_worktree(repo, 1, "auth")
        assert wt

        feature = Feature(name="auth", tasks=[_task("t1")])
        runner = MockRunner()

        result = await execute_feature(
            feature, runner, cwd=repo, feature_worktree=wt
        )

        assert result.passed
        assert runner.cwds[0] == wt.dir
        assert runner.cwds[0] != repo

        await cleanup_worktrees(repo, [wt])

    @pytest.mark.asyncio
    async def test_without_worktree_uses_cwd(self, tmp_path):
        """Without a feature worktree, tasks run in the given cwd."""
        repo = _init_repo(str(tmp_path / "repo"))

        feature = Feature(name="auth", tasks=[_task("t1")])
        runner = MockRunner()

        result = await execute_feature(feature, runner, cwd=repo)
        assert result.passed
        assert runner.cwds[0] == repo

    @pytest.mark.asyncio
    async def test_parallel_tasks_get_sub_worktrees(self, tmp_path):
        """Parallel tasks (same DAG level) in a feature worktree get separate dirs."""
        repo = _init_repo(str(tmp_path / "repo"))
        wt = await create_feature_worktree(repo, 1, "auth")
        assert wt

        # Two independent tasks → same level → sub-worktrees
        feature = Feature(name="auth", tasks=[_task("t1"), _task("t2")])
        runner = MockRunner()

        result = await execute_feature(
            feature, runner, cwd=repo, feature_worktree=wt, wave_num=1
        )

        assert result.passed
        assert len(runner.cwds) == 2
        # Each task should get a different directory (sub-worktrees)
        assert runner.cwds[0] != runner.cwds[1]
        # Neither should be the feature worktree itself
        assert runner.cwds[0] != wt.dir or runner.cwds[1] != wt.dir

        await cleanup_worktrees(repo, [wt])

    @pytest.mark.asyncio
    async def test_sequential_tasks_no_sub_worktrees(self, tmp_path):
        """Sequential tasks (chain dependencies) don't need sub-worktrees."""
        repo = _init_repo(str(tmp_path / "repo"))
        wt = await create_feature_worktree(repo, 1, "auth")
        assert wt

        feature = Feature(
            name="auth",
            tasks=[_task("t1"), _task("t2", depends=["t1"])],
        )
        runner = MockRunner()

        result = await execute_feature(
            feature, runner, cwd=repo, feature_worktree=wt, wave_num=1
        )

        assert result.passed
        # Both tasks run in the feature worktree itself (sequential, no sub-wt needed)
        assert runner.cwds[0] == wt.dir
        assert runner.cwds[1] == wt.dir

        await cleanup_worktrees(repo, [wt])

    @pytest.mark.asyncio
    async def test_sub_worktree_files_merged_back(self, tmp_path):
        """Files written in sub-worktrees should be merged into the feature branch."""
        repo = _init_repo(str(tmp_path / "repo"))
        wt = await create_feature_worktree(repo, 1, "auth")
        assert wt

        feature = Feature(name="auth", tasks=[_task("t1"), _task("t2")])
        runner = MockRunner(write_files=True)

        result = await execute_feature(
            feature, runner, cwd=repo, feature_worktree=wt, wave_num=1
        )

        assert result.passed
        # After merge, feature worktree should have both files
        assert os.path.isfile(os.path.join(wt.dir, "t1.py"))
        assert os.path.isfile(os.path.join(wt.dir, "t2.py"))

        await cleanup_worktrees(repo, [wt])

    @pytest.mark.asyncio
    async def test_worktree_prompt_includes_isolation_note(self, tmp_path):
        """Tasks in a worktree should get an isolation note in the prompt."""
        repo = _init_repo(str(tmp_path / "repo"))
        wt = await create_feature_worktree(repo, 1, "auth")
        assert wt

        prompts = []

        class PromptCapturingRunner:
            async def spawn(self, config: RunnerConfig) -> RunnerResult:
                prompts.append(config.prompt)
                return RunnerResult(
                    exit_code=0,
                    stdout=json.dumps({"type": "result", "result": "done"}),
                    stderr="",
                )

            def extract_final_output(self, stdout: str) -> str:
                return "done"

        feature = Feature(name="auth", tasks=[_task("t1")])
        result = await execute_feature(
            feature, PromptCapturingRunner(), cwd=repo, feature_worktree=wt
        )

        assert result.passed
        assert len(prompts) == 1
        assert "isolated git worktree" in prompts[0].lower()
        assert "do not run git checkout" in prompts[0].lower()

        await cleanup_worktrees(repo, [wt])

    @pytest.mark.asyncio
    async def test_branch_set_in_result(self, tmp_path):
        """Feature result should include the worktree branch name."""
        repo = _init_repo(str(tmp_path / "repo"))
        wt = await create_feature_worktree(repo, 1, "auth")
        assert wt

        feature = Feature(name="auth", tasks=[_task("t1")])
        runner = MockRunner()

        result = await execute_feature(
            feature, runner, cwd=repo, feature_worktree=wt
        )

        assert result.branch == "wave-1/auth"

        await cleanup_worktrees(repo, [wt])

    @pytest.mark.asyncio
    async def test_failed_dep_skips_dependent_tasks(self, tmp_path):
        """DAG failure cascading works correctly with worktrees."""
        repo = _init_repo(str(tmp_path / "repo"))
        wt = await create_feature_worktree(repo, 1, "auth")
        assert wt

        feature = Feature(
            name="auth",
            tasks=[
                _task("t1"),
                _task("t2", depends=["t1"]),
                _task("t3", depends=["t2"]),
            ],
        )
        runner = MockRunner(results={"t1": 1})

        result = await execute_feature(
            feature, runner, cwd=repo, feature_worktree=wt
        )

        assert not result.passed
        assert "t1" in runner.spawned
        assert "t2" not in runner.spawned
        assert "t3" not in runner.spawned

        await cleanup_worktrees(repo, [wt])


# ═══════════════════════════════════════════════════════════════
# WAVE EXECUTOR WITH WORKTREE ISOLATION
# ═══════════════════════════════════════════════════════════════


class TestWaveExecutorWithWorktrees:
    """Test the wave executor's worktree creation and merge lifecycle."""

    @pytest.mark.asyncio
    async def test_features_get_separate_worktrees(self, tmp_path):
        """Multiple named features should get separate working directories."""
        repo = _init_repo(str(tmp_path / "repo"))

        wave = Wave(
            name="W1",
            features=[
                Feature(name="auth", tasks=[_task("a1")]),
                Feature(name="billing", tasks=[_task("b1")]),
            ],
        )
        runner = MockRunner()

        result = await execute_wave(
            WaveExecutorOptions(
                wave=wave,
                wave_num=1,
                runner=runner,
                cwd=repo,
                repo_root=repo,
                use_worktrees=True,
            )
        )

        assert result.passed
        assert len(runner.cwds) == 2
        # Each feature ran in a different directory
        assert runner.cwds[0] != runner.cwds[1]

    @pytest.mark.asyncio
    async def test_single_default_feature_no_worktree(self, tmp_path):
        """Single 'default' feature should not create worktrees."""
        repo = _init_repo(str(tmp_path / "repo"))

        wave = Wave(
            name="W1",
            features=[Feature(name="default", tasks=[_task("t1")])],
        )
        runner = MockRunner()

        result = await execute_wave(
            WaveExecutorOptions(
                wave=wave,
                wave_num=1,
                runner=runner,
                cwd=repo,
                repo_root=repo,
                use_worktrees=True,
            )
        )

        assert result.passed
        # Task should run in the repo itself, not a worktree
        assert runner.cwds[0] == repo

    @pytest.mark.asyncio
    async def test_use_worktrees_false_runs_sequentially(self, tmp_path):
        """With use_worktrees=False, features run in the main cwd."""
        repo = _init_repo(str(tmp_path / "repo"))

        wave = Wave(
            name="W1",
            features=[
                Feature(name="auth", tasks=[_task("a1")]),
                Feature(name="billing", tasks=[_task("b1")]),
            ],
        )
        runner = MockRunner()

        result = await execute_wave(
            WaveExecutorOptions(
                wave=wave,
                wave_num=1,
                runner=runner,
                cwd=repo,
                use_worktrees=False,
            )
        )

        assert result.passed
        # Both should run in the same directory (no worktrees)
        assert runner.cwds[0] == repo
        assert runner.cwds[1] == repo

    @pytest.mark.asyncio
    async def test_worktree_files_merged_to_main(self, tmp_path):
        """Files written in feature worktrees should be merged back to the main repo."""
        repo = _init_repo(str(tmp_path / "repo"))

        wave = Wave(
            name="W1",
            features=[
                Feature(name="auth", tasks=[_task("a1")]),
                Feature(name="billing", tasks=[_task("b1")]),
            ],
        )
        runner = MockRunner(write_files=True)

        result = await execute_wave(
            WaveExecutorOptions(
                wave=wave,
                wave_num=1,
                runner=runner,
                cwd=repo,
                repo_root=repo,
                use_worktrees=True,
            )
        )

        assert result.passed
        # Files should be in the main repo after merge
        assert os.path.isfile(os.path.join(repo, "a1.py"))
        assert os.path.isfile(os.path.join(repo, "b1.py"))

    @pytest.mark.asyncio
    async def test_original_branch_unchanged(self, tmp_path):
        """The main repo's branch should not change during execution."""
        repo = _init_repo(str(tmp_path / "repo"))
        original_branch = await get_current_branch(repo)

        wave = Wave(
            name="W1",
            features=[
                Feature(name="auth", tasks=[_task("a1")]),
            ],
        )
        runner = MockRunner()

        await execute_wave(
            WaveExecutorOptions(
                wave=wave,
                wave_num=1,
                runner=runner,
                cwd=repo,
                repo_root=repo,
                use_worktrees=True,
            )
        )

        assert await get_current_branch(repo) == original_branch

    @pytest.mark.asyncio
    async def test_feature_failure_still_cleans_up(self, tmp_path):
        """Failed features should not leave dangling worktree directories."""
        repo = _init_repo(str(tmp_path / "repo"))

        wave = Wave(
            name="W1",
            features=[
                Feature(name="auth", tasks=[_task("a1")]),
                Feature(name="billing", tasks=[_task("b1")]),
            ],
        )
        runner = MockRunner(results={"b1": 1})

        result = await execute_wave(
            WaveExecutorOptions(
                wave=wave,
                wave_num=1,
                runner=runner,
                cwd=repo,
                repo_root=repo,
                use_worktrees=True,
            )
        )

        assert not result.passed
        # Worktree directories should be cleaned up
        wave_wt_dir = os.path.join(repo, ".wave-worktrees")
        if os.path.exists(wave_wt_dir):
            remaining = os.listdir(wave_wt_dir)
            assert len(remaining) == 0, f"Leftover worktree dirs: {remaining}"

    @pytest.mark.asyncio
    async def test_foundation_features_integration_with_worktrees(self, tmp_path):
        """Full lifecycle: foundation → features (worktrees) → merge → integration."""
        repo = _init_repo(str(tmp_path / "repo"))

        wave = Wave(
            name="W1",
            foundation=[_task("f1")],
            features=[
                Feature(name="auth", tasks=[_task("a1")]),
                Feature(name="billing", tasks=[_task("b1")]),
            ],
            integration=[_task("i1")],
        )
        runner = MockRunner()
        phases_seen = []

        result = await execute_wave(
            WaveExecutorOptions(
                wave=wave,
                wave_num=1,
                runner=runner,
                cwd=repo,
                repo_root=repo,
                use_worktrees=True,
                on_progress=lambda p: phases_seen.append(p.phase),
            )
        )

        assert result.passed
        assert "foundation" in phases_seen
        assert "features" in phases_seen
        assert "merge" in phases_seen
        assert "integration" in phases_seen

    @pytest.mark.asyncio
    async def test_merge_result_callback_fired(self, tmp_path):
        """on_merge_result should be called for each feature merge."""
        repo = _init_repo(str(tmp_path / "repo"))

        wave = Wave(
            name="W1",
            features=[
                Feature(name="auth", tasks=[_task("a1")]),
                Feature(name="billing", tasks=[_task("b1")]),
            ],
        )
        runner = MockRunner(write_files=True)
        merge_results = []

        result = await execute_wave(
            WaveExecutorOptions(
                wave=wave,
                wave_num=1,
                runner=runner,
                cwd=repo,
                repo_root=repo,
                use_worktrees=True,
                on_merge_result=lambda mr: merge_results.append(mr),
            )
        )

        assert result.passed
        assert len(merge_results) == 2
        assert all(mr.success for mr in merge_results)


# ═══════════════════════════════════════════════════════════════
# NON-GIT FALLBACK
# ═══════════════════════════════════════════════════════════════


class TestNonGitFallback:
    """When the project is not a git repo, worktrees should be skipped gracefully."""

    @pytest.mark.asyncio
    async def test_non_git_cwd_runs_normally(self, tmp_path):
        non_repo = str(tmp_path / "project")
        os.makedirs(non_repo)

        wave = Wave(
            name="W1",
            features=[
                Feature(name="auth", tasks=[_task("a1")]),
                Feature(name="billing", tasks=[_task("b1")]),
            ],
        )
        runner = MockRunner()

        result = await execute_wave(
            WaveExecutorOptions(
                wave=wave,
                wave_num=1,
                runner=runner,
                cwd=non_repo,
                repo_root=non_repo,
                use_worktrees=True,  # Enabled, but no git → falls back
            )
        )

        assert result.passed
        # Both ran in the same non-git directory
        assert runner.cwds[0] == non_repo
        assert runner.cwds[1] == non_repo
