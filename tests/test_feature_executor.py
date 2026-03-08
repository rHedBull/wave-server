"""Tests for the standalone feature executor."""

import json

import pytest

from wave_server.engine.feature_executor import execute_feature
from wave_server.engine.runner import PiRunner
from wave_server.engine.types import Feature, RunnerConfig, RunnerResult, Task, TaskResult


class MockRunner:
    def __init__(self, results: dict[str, int] | None = None):
        self.results = results or {}
        self.spawned: list[str] = []
        self.cwds: list[str] = []

    async def spawn(self, config: RunnerConfig) -> RunnerResult:
        self.spawned.append(config.task_id)
        self.cwds.append(config.cwd)
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


def _task(id: str, depends: list[str] | None = None) -> Task:
    return Task(id=id, title=f"Task {id}", depends=depends or [])


# ── Basic execution ────────────────────────────────────────────


class TestFeatureExecution:
    @pytest.mark.asyncio
    async def test_all_tasks_pass(self):
        feature = Feature(name="auth", tasks=[_task("t1"), _task("t2")])
        runner = MockRunner()
        result = await execute_feature(feature, runner)
        assert result.passed
        assert result.name == "auth"
        assert len(result.task_results) == 2
        assert all(r.exit_code == 0 for r in result.task_results)

    @pytest.mark.asyncio
    async def test_task_failure_marks_feature_failed(self):
        feature = Feature(name="auth", tasks=[_task("t1"), _task("t2")])
        runner = MockRunner(results={"t1": 1})
        result = await execute_feature(feature, runner)
        assert not result.passed

    @pytest.mark.asyncio
    async def test_dependent_task_skipped_on_failure(self):
        feature = Feature(
            name="auth",
            tasks=[_task("t1"), _task("t2", depends=["t1"])],
        )
        runner = MockRunner(results={"t1": 1})
        result = await execute_feature(feature, runner)
        assert not result.passed
        assert result.task_results[0].exit_code == 1  # t1 failed
        assert result.task_results[1].exit_code == -1  # t2 skipped
        assert "t2" not in runner.spawned

    @pytest.mark.asyncio
    async def test_empty_feature_passes(self):
        feature = Feature(name="empty", tasks=[])
        runner = MockRunner()
        result = await execute_feature(feature, runner)
        assert result.passed
        assert result.task_results == []

    @pytest.mark.asyncio
    async def test_feature_name_in_result(self):
        feature = Feature(name="my-feature", tasks=[_task("t1")])
        runner = MockRunner()
        result = await execute_feature(feature, runner)
        assert result.name == "my-feature"


# ── DAG ordering ───────────────────────────────────────────────


class TestDAGOrdering:
    @pytest.mark.asyncio
    async def test_dependencies_respected(self):
        feature = Feature(
            name="ordered",
            tasks=[
                _task("t3", depends=["t2"]),
                _task("t1"),
                _task("t2", depends=["t1"]),
            ],
        )
        runner = MockRunner()
        result = await execute_feature(feature, runner)
        assert result.passed
        # t1 must run before t2, t2 before t3
        assert runner.spawned.index("t1") < runner.spawned.index("t2")
        assert runner.spawned.index("t2") < runner.spawned.index("t3")

    @pytest.mark.asyncio
    async def test_parallel_independent_tasks(self):
        feature = Feature(
            name="parallel",
            tasks=[_task("a"), _task("b"), _task("c")],
        )
        runner = MockRunner()
        result = await execute_feature(feature, runner)
        assert result.passed
        assert set(runner.spawned) == {"a", "b", "c"}


# ── Skip task IDs ─────────────────────────────────────────────


class TestSkipTasks:
    @pytest.mark.asyncio
    async def test_skip_completed_tasks(self):
        feature = Feature(
            name="resume",
            tasks=[_task("t1"), _task("t2")],
        )
        runner = MockRunner()
        result = await execute_feature(
            feature, runner, skip_task_ids={"t1"}
        )
        assert result.passed
        assert "t1" not in runner.spawned
        assert "t2" in runner.spawned

    @pytest.mark.asyncio
    async def test_skipped_task_has_zero_exit_code(self):
        feature = Feature(name="resume", tasks=[_task("t1")])
        runner = MockRunner()
        result = await execute_feature(
            feature, runner, skip_task_ids={"t1"}
        )
        assert result.task_results[0].exit_code == 0
        assert "Resumed" in result.task_results[0].output


# ── Callbacks ──────────────────────────────────────────────────


class TestCallbacks:
    @pytest.mark.asyncio
    async def test_on_task_start_fires(self):
        started = []
        feature = Feature(name="cb", tasks=[_task("t1"), _task("t2")])
        runner = MockRunner()
        await execute_feature(
            feature,
            runner,
            on_task_start=lambda task: started.append(task.id),
        )
        assert "t1" in started
        assert "t2" in started

    @pytest.mark.asyncio
    async def test_on_task_end_fires_with_result(self):
        ended = []
        feature = Feature(name="cb", tasks=[_task("t1")])
        runner = MockRunner()
        await execute_feature(
            feature,
            runner,
            on_task_end=lambda task, result: ended.append(
                (task.id, result.exit_code)
            ),
        )
        assert ("t1", 0) in ended

    @pytest.mark.asyncio
    async def test_callbacks_fire_for_skipped_tasks(self):
        started = []
        ended = []
        feature = Feature(name="cb", tasks=[_task("t1")])
        runner = MockRunner()
        await execute_feature(
            feature,
            runner,
            skip_task_ids={"t1"},
            on_task_start=lambda task: started.append(task.id),
            on_task_end=lambda task, result: ended.append(task.id),
        )
        assert "t1" in started
        assert "t1" in ended


# ── CWD propagation ───────────────────────────────────────────


class TestCwd:
    @pytest.mark.asyncio
    async def test_cwd_passed_to_runner(self):
        feature = Feature(name="cwd", tasks=[_task("t1")])
        runner = MockRunner()
        await execute_feature(feature, runner, cwd="/my/project")
        assert runner.cwds == ["/my/project"]

    @pytest.mark.asyncio
    async def test_default_cwd_is_dot(self):
        feature = Feature(name="cwd", tasks=[_task("t1")])
        runner = MockRunner()
        await execute_feature(feature, runner)
        assert runner.cwds == ["."]


# ── Rate limit detection (PiRunner integration) ───────────────


def _build_rate_limited_pi_output() -> str:
    """Build realistic pi JSON output that simulates a rate-limited task.

    Pi exits 0 even when all retries fail. The output contains:
    - agent_end with stopReason=error and errorMessage=429
    - auto_retry_end with success=false
    """
    return "\n".join([
        json.dumps({"type": "session", "version": 3, "id": "test-session"}),
        json.dumps({"type": "agent_start"}),
        json.dumps({"type": "turn_start"}),
        json.dumps({
            "type": "message_end",
            "message": {
                "role": "assistant", "content": [],
                "stopReason": "error",
                "errorMessage": '429 {"type":"error","error":{"type":"rate_limit_error","message":"Rate limit exceeded"}}',
            },
        }),
        json.dumps({
            "type": "turn_end",
            "message": {
                "role": "assistant", "content": [],
                "stopReason": "error",
                "errorMessage": '429 {"type":"error","error":{"type":"rate_limit_error","message":"Rate limit exceeded"}}',
            },
            "toolResults": [],
        }),
        json.dumps({
            "type": "agent_end",
            "messages": [{
                "role": "assistant", "content": [],
                "stopReason": "error",
                "errorMessage": '429 {"type":"error","error":{"type":"rate_limit_error","message":"Rate limit exceeded"}}',
            }],
        }),
        json.dumps({
            "type": "auto_retry_start",
            "attempt": 3, "maxAttempts": 3, "delayMs": 8000,
            "errorMessage": "429 rate_limit_error",
        }),
        json.dumps({"type": "agent_start"}),
        json.dumps({"type": "turn_start"}),
        json.dumps({
            "type": "message_end",
            "message": {
                "role": "assistant", "content": [],
                "stopReason": "error",
                "errorMessage": '429 {"type":"error","error":{"type":"rate_limit_error","message":"Rate limit exceeded"}}',
            },
        }),
        json.dumps({
            "type": "turn_end",
            "message": {
                "role": "assistant", "content": [],
                "stopReason": "error",
                "errorMessage": '429 {"type":"error","error":{"type":"rate_limit_error","message":"Rate limit exceeded"}}',
            },
            "toolResults": [],
        }),
        json.dumps({
            "type": "agent_end",
            "messages": [{
                "role": "assistant", "content": [],
                "stopReason": "error",
                "errorMessage": '429 {"type":"error","error":{"type":"rate_limit_error","message":"Rate limit exceeded"}}',
            }],
        }),
        json.dumps({
            "type": "auto_retry_end",
            "success": False,
            "attempt": 3,
            "finalError": '429 {"type":"error","error":{"type":"rate_limit_error","message":"Rate limit exceeded"}}',
        }),
    ])


class RateLimitPiMockRunner:
    """Mock runner that simulates PiRunner behavior: exit code 0 but rate-limited output.

    Uses the real PiRunner's output failure detection logic by wrapping
    _detect_pi_output_failure and extract_final_output.
    """

    def __init__(self, rate_limited_task_ids: set[str]):
        self.rate_limited_task_ids = rate_limited_task_ids
        self.spawned: list[str] = []
        self._pi_runner = PiRunner()

    async def spawn(self, config: RunnerConfig) -> RunnerResult:
        from wave_server.engine.runner import _detect_pi_output_failure

        self.spawned.append(config.task_id)

        if config.task_id in self.rate_limited_task_ids:
            stdout = _build_rate_limited_pi_output()
            # Pi exits 0 despite rate limiting
            exit_code = 0
            stderr = ""

            # Apply the same detection logic PiRunner.spawn() uses
            detected = _detect_pi_output_failure(stdout)
            if detected:
                exit_code = 1
                stderr = detected

            return RunnerResult(
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
            )

        # Normal success
        stdout = json.dumps({
            "type": "agent_end",
            "messages": [{
                "role": "assistant",
                "content": [{"type": "text", "text": f"Completed task {config.task_id}"}],
                "stopReason": "stop",
            }],
        })
        return RunnerResult(exit_code=0, stdout=stdout, stderr="")

    def extract_final_output(self, stdout: str) -> str:
        return self._pi_runner.extract_final_output(stdout)


class TestRateLimitDetection:
    """Integration tests: rate-limited pi tasks are correctly marked as failed
    in the feature executor pipeline."""

    @pytest.mark.asyncio
    async def test_rate_limited_task_fails(self):
        """A single rate-limited task should be marked as failed."""
        feature = Feature(name="test", tasks=[_task("t1")])
        runner = RateLimitPiMockRunner(rate_limited_task_ids={"t1"})

        result = await execute_feature(feature, runner)

        assert not result.passed
        assert result.task_results[0].exit_code == 1
        assert "retries exhausted" in result.task_results[0].stderr

    @pytest.mark.asyncio
    async def test_rate_limited_task_blocks_dependents(self):
        """Tasks depending on a rate-limited task should be skipped."""
        feature = Feature(
            name="test",
            tasks=[
                _task("t1"),
                _task("t2", depends=["t1"]),
                _task("t3", depends=["t2"]),
            ],
        )
        runner = RateLimitPiMockRunner(rate_limited_task_ids={"t1"})

        result = await execute_feature(feature, runner)

        assert not result.passed
        assert len(result.task_results) == 3
        # t1 failed (rate limited)
        assert result.task_results[0].id == "t1"
        assert result.task_results[0].exit_code == 1
        # t2 skipped (dependency failed)
        assert result.task_results[1].id == "t2"
        assert result.task_results[1].exit_code == -1
        # t3 skipped (dependency chain failed)
        assert result.task_results[2].id == "t3"
        assert result.task_results[2].exit_code == -1
        # Only t1 was spawned
        assert runner.spawned == ["t1"]

    @pytest.mark.asyncio
    async def test_mix_of_rate_limited_and_successful(self):
        """Some tasks rate-limited, others succeed — feature should fail."""
        feature = Feature(
            name="test",
            tasks=[
                _task("t1"),  # succeeds
                _task("t2"),  # rate limited
            ],
        )
        runner = RateLimitPiMockRunner(rate_limited_task_ids={"t2"})

        result = await execute_feature(feature, runner)

        assert not result.passed
        t1_result = next(r for r in result.task_results if r.id == "t1")
        t2_result = next(r for r in result.task_results if r.id == "t2")
        assert t1_result.exit_code == 0
        assert t2_result.exit_code == 1

    @pytest.mark.asyncio
    async def test_successful_task_still_passes(self):
        """When no tasks are rate-limited, everything passes normally."""
        feature = Feature(
            name="test",
            tasks=[_task("t1"), _task("t2")],
        )
        runner = RateLimitPiMockRunner(rate_limited_task_ids=set())

        result = await execute_feature(feature, runner)

        assert result.passed
        assert all(r.exit_code == 0 for r in result.task_results)
