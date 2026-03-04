"""Tests for the structured execution-level logger."""

import time
from unittest.mock import patch

import pytest

from wave_server.engine.execution_logger import (
    ExecutionLogger,
    TaskRecord,
    WaveRecord,
    _duration_str,
    _elapsed_str,
    _status_icon,
    _agent_icon,
)
from wave_server.engine.types import Task, TaskResult


# ── Helper Factories ────────────────────────────────────────────────


def _task(id: str = "t1", title: str = "Do thing", agent: str = "worker") -> Task:
    return Task(id=id, title=title, agent=agent, files=[], depends=[])


def _result(
    exit_code: int = 0,
    duration_ms: int = 5000,
    timed_out: bool = False,
    stderr: str = "",
    stdout: str = "",
    output: str = "",
) -> TaskResult:
    return TaskResult(
        id="t1",
        title="Do thing",
        agent="worker",
        exit_code=exit_code,
        duration_ms=duration_ms,
        timed_out=timed_out,
        stderr=stderr,
        stdout=stdout,
        output=output,
    )


def _make_logger(**kwargs) -> ExecutionLogger:
    defaults = {
        "execution_id": "exec-123",
        "runtime": "claude",
        "total_tasks": 10,
        "max_concurrency": 4,
        "goal": "Build the app",
        "wave_count": 2,
    }
    defaults.update(kwargs)
    return ExecutionLogger(**defaults)


# ── Utility Function Tests ──────────────────────────────────────────


class TestDurationStr:
    def test_milliseconds(self):
        assert _duration_str(500) == "500ms"

    def test_seconds(self):
        assert _duration_str(5000) == "5.0s"

    def test_minutes(self):
        assert _duration_str(125000) == "2m 05s"

    def test_zero(self):
        assert _duration_str(0) == "0ms"


class TestStatusIcon:
    def test_success(self):
        assert _status_icon(0, False) == "✅"

    def test_failure(self):
        assert _status_icon(1, False) == "❌"

    def test_skipped(self):
        assert _status_icon(-1, False) == "⏭️"

    def test_timed_out(self):
        assert _status_icon(1, True) == "⏰"

    def test_timed_out_overrides_success(self):
        assert _status_icon(0, True) == "⏰"


class TestAgentIcon:
    def test_worker(self):
        assert _agent_icon("worker") == "🔨"

    def test_test_writer(self):
        assert _agent_icon("test-writer") == "🧪"

    def test_verifier(self):
        assert _agent_icon("wave-verifier") == "🔍"

    def test_unknown(self):
        assert _agent_icon("custom") == "🔨"


# ── ExecutionLogger Tests ───────────────────────────────────────────


class TestExecutionStarted:
    def test_header_content(self):
        logger = _make_logger()
        logger.execution_started()
        text = logger.render()
        assert "# Execution Log" in text
        assert "exec-123" in text
        assert "Runtime: claude" in text
        assert "Concurrency: 4" in text
        assert "Goal: Build the app" in text
        assert "2 waves, 10 tasks" in text

    def test_no_goal(self):
        logger = _make_logger(goal="")
        logger.execution_started()
        text = logger.render()
        assert "Goal:" not in text


class TestWaveLifecycle:
    def test_wave_started(self):
        logger = _make_logger()
        logger.execution_started()
        logger.wave_started("Setup", 0)
        text = logger.render()
        assert "Wave 1: Setup" in text

    def test_wave_ended_passed(self):
        logger = _make_logger()
        logger.execution_started()
        logger.wave_started("Setup", 0)
        logger.wave_ended("Setup", 0, passed=True)
        text = logger.render()
        assert "✅" in text
        assert "passed" in text

    def test_wave_ended_failed(self):
        logger = _make_logger()
        logger.execution_started()
        logger.wave_started("Setup", 0)
        logger.wave_ended("Setup", 0, passed=False)
        text = logger.render()
        assert "❌" in text
        assert "FAILED" in text

    def test_wave_records_tracked(self):
        logger = _make_logger()
        logger.execution_started()
        logger.wave_started("W1", 0)
        logger.wave_ended("W1", 0, passed=True)
        logger.wave_started("W2", 1)
        logger.wave_ended("W2", 1, passed=False)
        assert len(logger._waves) == 2
        assert logger._waves[0].passed is True
        assert logger._waves[1].passed is False


class TestPhaseEvents:
    def test_phase_started(self):
        logger = _make_logger()
        logger.execution_started()
        logger.phase_started("foundation")
        text = logger.render()
        assert "Foundation" in text

    def test_phase_skipped(self):
        logger = _make_logger()
        logger.execution_started()
        logger.phase_skipped("integration", "features failed")
        text = logger.render()
        assert "⏭️" in text
        assert "skipped" in text
        assert "features failed" in text


class TestTaskEvents:
    def test_task_started(self):
        logger = _make_logger()
        logger.execution_started()
        logger.wave_started("W1", 0)
        logger.task_started("foundation", _task(id="t1", title="Init DB"))
        text = logger.render()
        assert "▶" in text
        assert "t1" in text
        assert "Init DB" in text

    def test_task_ended_success(self):
        logger = _make_logger()
        logger.execution_started()
        logger.wave_started("W1", 0)
        logger.task_started("foundation", _task())
        logger.task_ended("foundation", _task(), _result(exit_code=0, duration_ms=3000))
        text = logger.render()
        assert "✅" in text
        assert "3.0s" in text

    def test_task_ended_failure(self):
        logger = _make_logger()
        logger.execution_started()
        logger.wave_started("W1", 0)
        logger.task_ended("foundation", _task(), _result(exit_code=1, stderr="TypeError: bad"))
        text = logger.render()
        assert "❌" in text
        assert "TypeError: bad" in text

    def test_task_timed_out(self):
        logger = _make_logger()
        logger.execution_started()
        logger.wave_started("W1", 0)
        logger.task_ended("foundation", _task(), _result(timed_out=True, exit_code=1))
        text = logger.render()
        assert "⏰" in text
        assert "TIMED OUT" in text

    def test_task_skipped(self):
        logger = _make_logger()
        logger.execution_started()
        logger.wave_started("W1", 0)
        logger.task_ended("foundation", _task(), _result(exit_code=-1))
        text = logger.render()
        assert "⏭️" in text

    def test_task_recorded_in_wave(self):
        logger = _make_logger()
        logger.execution_started()
        logger.wave_started("W1", 0)
        logger.task_ended("foundation", _task(id="t1"), _result(exit_code=0))
        logger.task_ended("foundation", _task(id="t2"), _result(exit_code=1))
        logger.wave_ended("W1", 0, passed=False)
        assert len(logger._waves[0].tasks) == 2
        assert logger._waves[0].tasks[0].exit_code == 0
        assert logger._waves[0].tasks[1].exit_code == 1

    def test_agent_icons(self):
        logger = _make_logger()
        logger.execution_started()
        logger.wave_started("W1", 0)
        logger.task_started("foundation", _task(agent="worker"))
        logger.task_started("foundation", _task(agent="test-writer"))
        logger.task_started("foundation", _task(agent="wave-verifier"))
        text = logger.render()
        assert "🔨" in text
        assert "🧪" in text
        assert "🔍" in text


class TestFeatureEvents:
    def test_feature_started_non_default(self):
        logger = _make_logger()
        logger.execution_started()
        logger.feature_started("auth")
        text = logger.render()
        assert "Feature: auth" in text

    def test_feature_started_default_silent(self):
        logger = _make_logger()
        logger.execution_started()
        logger.feature_started("default")
        text = logger.render()
        assert "Feature:" not in text

    def test_feature_ended(self):
        logger = _make_logger()
        logger.execution_started()
        logger.feature_started("auth")
        logger.feature_ended("auth", True)
        text = logger.render()
        assert "✅" in text
        assert "'auth'" in text

    def test_feature_failed(self):
        logger = _make_logger()
        logger.execution_started()
        logger.feature_started("auth")
        logger.feature_ended("auth", False)
        text = logger.render()
        assert "❌" in text
        assert "FAILED" in text


class TestCostTracking:
    def test_add_cost(self):
        logger = _make_logger()
        logger.execution_started()
        logger.add_cost(0.05, 1000, 500)
        logger.add_cost(0.03, 800, 300)
        assert logger._total_cost == pytest.approx(0.08)
        assert logger._total_input_tokens == 1800
        assert logger._total_output_tokens == 800


class TestExecutionFinished:
    def _run_full_execution(self, *, fail: bool = False) -> ExecutionLogger:
        logger = _make_logger(total_tasks=3)
        logger.execution_started()

        logger.wave_started("W1", 0)
        t1 = _task(id="t1", title="Init DB")
        t2 = _task(id="t2", title="Auth service")
        t3 = _task(id="t3", title="Integration test", agent="wave-verifier")

        logger.task_started("foundation", t1)
        logger.task_ended("foundation", t1, _result(exit_code=0, duration_ms=2000))

        logger.task_started("feature:auth", t2)
        if fail:
            logger.task_ended("feature:auth", t2, _result(exit_code=1, duration_ms=5000, stderr="AssertionError: bad"))
        else:
            logger.task_ended("feature:auth", t2, _result(exit_code=0, duration_ms=5000))

        logger.task_started("integration", t3)
        if fail:
            logger.task_ended("integration", t3, _result(exit_code=-1))  # skipped
        else:
            logger.task_ended("integration", t3, _result(exit_code=0, duration_ms=3000))

        logger.wave_ended("W1", 0, passed=not fail)

        logger.add_cost(0.05, 2000, 500)
        logger.execution_finished(all_passed=not fail)
        return logger

    def test_success_summary(self):
        logger = self._run_full_execution(fail=False)
        text = logger.render()
        assert "# ✅ SUCCESS" in text
        assert "3 passed, 0 failed, 0 skipped" in text
        assert "Finished:" in text
        assert "Duration:" in text
        assert "$0.0500" in text
        assert "2,000 in" in text

    def test_failure_summary(self):
        logger = self._run_full_execution(fail=True)
        text = logger.render()
        assert "# ❌ FAILED" in text
        assert "1 passed, 1 failed, 1 skipped" in text

    def test_failed_task_details(self):
        logger = self._run_full_execution(fail=True)
        text = logger.render()
        assert "### Failed Tasks" in text
        assert "t2" in text
        assert "Auth service" in text

    def test_per_wave_summary(self):
        logger = self._run_full_execution(fail=False)
        text = logger.render()
        assert "**W1**" in text
        assert "3/3 tasks passed" in text

    def test_stopped_early_note(self):
        logger = _make_logger(total_tasks=5, wave_count=3)
        logger.execution_started()
        logger.wave_started("W1", 0)
        t = _task(id="t1")
        logger.task_ended("foundation", t, _result(exit_code=1))
        logger.wave_ended("W1", 0, passed=False)
        logger.execution_finished(all_passed=False)
        text = logger.render()
        assert "stopped at failure" in text
        assert "1/3" in text

    def test_no_cost_when_zero(self):
        logger = _make_logger(total_tasks=1)
        logger.execution_started()
        logger.wave_started("W1", 0)
        logger.task_ended("foundation", _task(), _result())
        logger.wave_ended("W1", 0, passed=True)
        logger.execution_finished(all_passed=True)
        text = logger.render()
        assert "Total cost:" not in text


class TestFreeFormLog:
    def test_log_with_timestamp(self):
        logger = _make_logger()
        logger.execution_started()
        logger.log("Custom event happened")
        text = logger.render()
        assert "Custom event happened" in text
        # Has timestamp prefix
        assert "[" in text.split("Custom event")[0]

    def test_log_raw(self):
        logger = _make_logger()
        logger.execution_started()
        logger.log_raw("---")
        text = logger.render()
        assert "---" in text


class TestRender:
    def test_render_returns_string(self):
        logger = _make_logger()
        logger.execution_started()
        assert isinstance(logger.render(), str)
        assert logger.render().endswith("\n")

    def test_render_lines(self):
        logger = _make_logger()
        logger.execution_started()
        lines = logger.render_lines()
        assert isinstance(lines, list)
        assert all(isinstance(l, str) for l in lines)

    def test_render_lines_is_copy(self):
        logger = _make_logger()
        logger.execution_started()
        lines = logger.render_lines()
        lines.append("extra")
        assert "extra" not in logger.render()


class TestElapsedStr:
    def test_format(self):
        # Mock time.monotonic to control elapsed time
        with patch("wave_server.engine.execution_logger.time") as mock_time:
            mock_time.monotonic.return_value = 125.0  # 2m 5s after start=0
            result = _elapsed_str(0.0)
            assert result == "[02:05]"

    def test_zero(self):
        with patch("wave_server.engine.execution_logger.time") as mock_time:
            mock_time.monotonic.return_value = 0.0
            result = _elapsed_str(0.0)
            assert result == "[00:00]"
