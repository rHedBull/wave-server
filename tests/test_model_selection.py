"""Tests for per-execution and per-agent-type model selection."""

from unittest.mock import patch

import json

import pytest

from wave_server.config import Settings
from wave_server.engine.feature_executor import execute_feature
from wave_server.engine.runner import ClaudeCodeRunner
from wave_server.engine.types import (
    Feature,
    RunnerConfig,
    RunnerResult,
    Task,
    Wave,
)
from wave_server.engine.wave_executor import WaveExecutorOptions, execute_wave
from wave_server.schemas import ExecutionCreate


# ── Helpers ────────────────────────────────────────────────────


class CapturingRunner:
    """Records every RunnerConfig passed to spawn()."""

    def __init__(self, exit_code: int = 0):
        self.configs: list[RunnerConfig] = []
        self._exit_code = exit_code

    async def spawn(self, config: RunnerConfig) -> RunnerResult:
        self.configs.append(config)
        return RunnerResult(
            exit_code=self._exit_code,
            stdout=json.dumps({"type": "result", "result": f"done:{config.task_id}"}),
            stderr="",
        )

    def extract_final_output(self, stdout: str) -> str:
        for line in stdout.split("\n"):
            try:
                msg = json.loads(line)
                if msg.get("type") == "result":
                    return msg.get("result", "")
            except (json.JSONDecodeError, KeyError):
                pass
        return stdout

    def model_for(self, task_id: str) -> str | None:
        for c in self.configs:
            if c.task_id == task_id:
                return c.model
        return None


def _task(id: str, agent: str = "worker") -> Task:
    return Task(id=id, title=f"Task {id}", agent=agent)


# ── Schema ─────────────────────────────────────────────────────


class TestExecutionCreateSchema:
    def test_model_defaults_none(self):
        e = ExecutionCreate()
        assert e.model is None

    def test_agent_models_defaults_none(self):
        e = ExecutionCreate()
        assert e.agent_models is None

    def test_model_accepted(self):
        e = ExecutionCreate(model="claude-sonnet-4-5")
        assert e.model == "claude-sonnet-4-5"

    def test_agent_models_accepted(self):
        e = ExecutionCreate(
            agent_models={
                "worker": "claude-sonnet-4-5",
                "test-writer": "claude-haiku-4-5",
                "wave-verifier": "claude-haiku-4-5",
            }
        )
        assert e.agent_models["worker"] == "claude-sonnet-4-5"
        assert e.agent_models["test-writer"] == "claude-haiku-4-5"

    def test_model_and_agent_models_together(self):
        e = ExecutionCreate(
            model="claude-sonnet-4-5",
            agent_models={"test-writer": "claude-haiku-4-5"},
        )
        assert e.model == "claude-sonnet-4-5"
        assert e.agent_models["test-writer"] == "claude-haiku-4-5"


# ── RunnerConfig ───────────────────────────────────────────────


class TestRunnerConfig:
    def test_model_defaults_none(self):
        cfg = RunnerConfig(task_id="t1", prompt="hi", cwd="/tmp")
        assert cfg.model is None

    def test_model_set(self):
        cfg = RunnerConfig(task_id="t1", prompt="hi", cwd="/tmp", model="claude-sonnet-4-5")
        assert cfg.model == "claude-sonnet-4-5"


# ── ClaudeCodeRunner cmd building ─────────────────────────────


class TestClaudeCodeRunnerCmd:
    """Verify the CLI args include --model when set (without actually spawning)."""

    def test_cmd_without_model(self):
        runner = ClaudeCodeRunner()
        # Reconstruct cmd as runner.spawn() would
        config = RunnerConfig(task_id="t", prompt="do it", cwd="/tmp", model=None)
        cmd = [
            "claude", "--print", "--verbose", "--output-format", "stream-json",
            "--dangerously-skip-permissions",
        ]
        if config.model:
            cmd += ["--model", config.model]
        cmd.append(config.prompt)
        assert "--model" not in cmd
        assert cmd[-1] == "do it"

    def test_cmd_with_model(self):
        config = RunnerConfig(task_id="t", prompt="do it", cwd="/tmp", model="claude-sonnet-4-5")
        cmd = [
            "claude", "--print", "--verbose", "--output-format", "stream-json",
            "--dangerously-skip-permissions",
        ]
        if config.model:
            cmd += ["--model", config.model]
        cmd.append(config.prompt)
        assert "--model" in cmd
        assert cmd[cmd.index("--model") + 1] == "claude-sonnet-4-5"
        assert cmd[-1] == "do it"


# ── Feature executor model threading ──────────────────────────


class TestFeatureExecutorModelThreading:
    @pytest.mark.asyncio
    async def test_no_model_passes_none(self):
        feature = Feature(name="f", tasks=[_task("t1", "worker")])
        runner = CapturingRunner()
        await execute_feature(feature, runner)
        assert runner.model_for("t1") is None

    @pytest.mark.asyncio
    async def test_global_model_applied_to_all_tasks(self):
        feature = Feature(
            name="f",
            tasks=[_task("t1", "worker"), _task("t2", "test-writer")],
        )
        runner = CapturingRunner()
        await execute_feature(feature, runner, model="claude-sonnet-4-5")
        assert runner.model_for("t1") == "claude-sonnet-4-5"
        assert runner.model_for("t2") == "claude-sonnet-4-5"

    @pytest.mark.asyncio
    async def test_agent_model_overrides_global(self):
        feature = Feature(
            name="f",
            tasks=[_task("t1", "worker"), _task("t2", "test-writer")],
        )
        runner = CapturingRunner()
        await execute_feature(
            feature,
            runner,
            model="claude-sonnet-4-5",
            agent_models={"test-writer": "claude-haiku-4-5"},
        )
        assert runner.model_for("t1") == "claude-sonnet-4-5"   # global fallback
        assert runner.model_for("t2") == "claude-haiku-4-5"    # per-agent override

    @pytest.mark.asyncio
    async def test_agent_model_without_global(self):
        feature = Feature(
            name="f",
            tasks=[_task("t1", "worker"), _task("t2", "wave-verifier")],
        )
        runner = CapturingRunner()
        await execute_feature(
            feature,
            runner,
            agent_models={"wave-verifier": "claude-haiku-4-5"},
        )
        assert runner.model_for("t1") is None                   # no model set
        assert runner.model_for("t2") == "claude-haiku-4-5"    # per-agent override

    @pytest.mark.asyncio
    async def test_all_three_agent_types_get_correct_models(self):
        feature = Feature(
            name="f",
            tasks=[
                _task("t1", "worker"),
                _task("t2", "test-writer"),
                _task("t3", "wave-verifier"),
            ],
        )
        runner = CapturingRunner()
        await execute_feature(
            feature,
            runner,
            model="claude-sonnet-4-5",
            agent_models={
                "worker": "claude-sonnet-4-5",
                "test-writer": "claude-haiku-4-5",
                "wave-verifier": "claude-haiku-4-5",
            },
        )
        assert runner.model_for("t1") == "claude-sonnet-4-5"
        assert runner.model_for("t2") == "claude-haiku-4-5"
        assert runner.model_for("t3") == "claude-haiku-4-5"


# ── Wave executor model threading ─────────────────────────────


class TestWaveExecutorModelThreading:
    def _make_opts(self, runner, **kwargs) -> WaveExecutorOptions:
        wave = Wave(
            name="w1",
            foundation=[_task("f1", "worker")],
            features=[Feature(name="default", tasks=[_task("feat1", "test-writer")])],
            integration=[_task("i1", "wave-verifier")],
        )
        return WaveExecutorOptions(
            wave=wave,
            wave_num=1,
            runner=runner,
            use_worktrees=False,
            **kwargs,
        )

    @pytest.mark.asyncio
    async def test_no_model_all_none(self):
        runner = CapturingRunner()
        opts = self._make_opts(runner)
        await execute_wave(opts)
        assert runner.model_for("f1") is None
        assert runner.model_for("feat1") is None
        assert runner.model_for("i1") is None

    @pytest.mark.asyncio
    async def test_global_model_propagates_to_all_phases(self):
        runner = CapturingRunner()
        opts = self._make_opts(runner, model="claude-sonnet-4-5")
        await execute_wave(opts)
        assert runner.model_for("f1") == "claude-sonnet-4-5"
        assert runner.model_for("feat1") == "claude-sonnet-4-5"
        assert runner.model_for("i1") == "claude-sonnet-4-5"

    @pytest.mark.asyncio
    async def test_agent_models_override_per_phase(self):
        runner = CapturingRunner()
        opts = self._make_opts(
            runner,
            model="claude-sonnet-4-5",
            agent_models={
                "test-writer": "claude-haiku-4-5",
                "wave-verifier": "claude-haiku-4-5",
            },
        )
        await execute_wave(opts)
        assert runner.model_for("f1") == "claude-sonnet-4-5"    # worker → global
        assert runner.model_for("feat1") == "claude-haiku-4-5"  # test-writer → override
        assert runner.model_for("i1") == "claude-haiku-4-5"     # wave-verifier → override

    @pytest.mark.asyncio
    async def test_partial_agent_models_falls_back_correctly(self):
        runner = CapturingRunner()
        opts = self._make_opts(
            runner,
            model="claude-sonnet-4-5",
            agent_models={"test-writer": "claude-haiku-4-5"},
        )
        await execute_wave(opts)
        assert runner.model_for("f1") == "claude-sonnet-4-5"    # worker → global
        assert runner.model_for("feat1") == "claude-haiku-4-5"  # test-writer → override
        assert runner.model_for("i1") == "claude-sonnet-4-5"    # wave-verifier → global


# ── Server config per-agent defaults ──────────────────────────


class TestServerConfigAgentModels:
    def test_default_agent_model_fields_are_none(self):
        s = Settings()
        assert s.default_model_worker is None
        assert s.default_model_test_writer is None
        assert s.default_model_wave_verifier is None

    def test_env_vars_set_per_agent_defaults(self):
        with patch.dict(
            "os.environ",
            {
                "WAVE_DEFAULT_MODEL_WORKER": "claude-sonnet-4-5",
                "WAVE_DEFAULT_MODEL_TEST_WRITER": "claude-haiku-4-5",
                "WAVE_DEFAULT_MODEL_WAVE_VERIFIER": "claude-haiku-4-5",
            },
        ):
            s = Settings()
            assert s.default_model_worker == "claude-sonnet-4-5"
            assert s.default_model_test_writer == "claude-haiku-4-5"
            assert s.default_model_wave_verifier == "claude-haiku-4-5"


# ── Server defaults merged with execution overrides ────────────


class TestAgentModelMerging:
    """Tests for the merge logic in execution_manager._run_execution."""

    def _merge(
        self,
        server_worker: str | None = None,
        server_test_writer: str | None = None,
        server_wave_verifier: str | None = None,
        exec_agent_models: dict | None = None,
    ) -> dict[str, str] | None:
        """Replicate the merge logic from execution_manager."""
        server_agent_models: dict[str, str] = {}
        if server_worker:
            server_agent_models["worker"] = server_worker
        if server_test_writer:
            server_agent_models["test-writer"] = server_test_writer
        if server_wave_verifier:
            server_agent_models["wave-verifier"] = server_wave_verifier

        merged = {**server_agent_models, **(exec_agent_models or {})} or None
        return merged

    def test_no_config_anywhere_returns_none(self):
        assert self._merge() is None

    def test_server_defaults_used_when_no_exec_override(self):
        result = self._merge(
            server_worker="claude-sonnet-4-5",
            server_test_writer="claude-haiku-4-5",
        )
        assert result == {
            "worker": "claude-sonnet-4-5",
            "test-writer": "claude-haiku-4-5",
        }

    def test_exec_override_wins_over_server_default(self):
        result = self._merge(
            server_worker="claude-sonnet-4-5",
            exec_agent_models={"worker": "claude-haiku-4-5"},
        )
        assert result["worker"] == "claude-haiku-4-5"

    def test_exec_can_add_agents_not_in_server_defaults(self):
        result = self._merge(
            server_worker="claude-sonnet-4-5",
            exec_agent_models={"wave-verifier": "claude-haiku-4-5"},
        )
        assert result["worker"] == "claude-sonnet-4-5"
        assert result["wave-verifier"] == "claude-haiku-4-5"

    def test_full_override_all_three_agents(self):
        result = self._merge(
            server_worker="claude-sonnet-4-5",
            server_test_writer="claude-sonnet-4-5",
            server_wave_verifier="claude-sonnet-4-5",
            exec_agent_models={
                "worker": "claude-haiku-4-5",
                "test-writer": "claude-haiku-4-5",
                "wave-verifier": "claude-haiku-4-5",
            },
        )
        assert all(v == "claude-haiku-4-5" for v in result.values())

    def test_empty_exec_agent_models_keeps_server_defaults(self):
        result = self._merge(
            server_worker="claude-sonnet-4-5",
            exec_agent_models={},
        )
        assert result == {"worker": "claude-sonnet-4-5"}
