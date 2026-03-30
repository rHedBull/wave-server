"""Tests for rate-limit pause-and-resume.

Covers:
- RateLimitPauser: gate semantics, idempotent pause, callback invocation
- RateLimitAwareRunner: retry loop, integration with pauser
- is_rate_limit_error: detection via flag and pattern matching
- _detect_pi_output_failure: rate_limited flag on PiOutputFailure
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from wave_server.engine.rate_limit import (
    RateLimitAwareRunner,
    RateLimitPauser,
    is_rate_limit_error,
)
from wave_server.engine.runner import (
    _detect_pi_output_failure,
    _is_rate_limit_message,
)
from wave_server.engine.types import RunnerConfig, RunnerResult


# ---------------------------------------------------------------------------
# is_rate_limit_error
# ---------------------------------------------------------------------------


class TestIsRateLimitError:
    def test_flag_true(self):
        r = RunnerResult(exit_code=1, stdout="", stderr="", rate_limited=True)
        assert is_rate_limit_error(r) is True

    def test_flag_false_no_patterns(self):
        r = RunnerResult(exit_code=1, stdout="", stderr="Something else failed")
        assert is_rate_limit_error(r) is False

    def test_exit_code_zero_not_rate_limited(self):
        """Successful results are never rate-limited even with suspicious text."""
        r = RunnerResult(exit_code=0, stdout="", stderr="429 rate_limit")
        assert is_rate_limit_error(r) is False

    def test_pattern_429_in_stderr(self):
        r = RunnerResult(exit_code=1, stdout="", stderr="Error: 429 Too Many Requests")
        assert is_rate_limit_error(r) is True

    def test_pattern_rate_limit_in_stdout(self):
        r = RunnerResult(
            exit_code=1, stdout="rate_limit_error: exceeded", stderr=""
        )
        assert is_rate_limit_error(r) is True

    def test_pattern_overloaded(self):
        r = RunnerResult(exit_code=1, stdout="", stderr="529 overloaded_error")
        assert is_rate_limit_error(r) is True

    def test_pattern_case_insensitive(self):
        r = RunnerResult(exit_code=1, stdout="", stderr="Rate Limit exceeded")
        assert is_rate_limit_error(r) is True


# ---------------------------------------------------------------------------
# _is_rate_limit_message (runner helper)
# ---------------------------------------------------------------------------


class TestIsRateLimitMessage:
    def test_rate_limit_error(self):
        assert _is_rate_limit_message("429 rate_limit_error") is True

    def test_overloaded(self):
        assert _is_rate_limit_message("529 overloaded") is True

    def test_clean_error(self):
        assert _is_rate_limit_message("TypeError: cannot read property") is False

    def test_too_many_requests(self):
        assert _is_rate_limit_message("Too Many Requests") is True


# ---------------------------------------------------------------------------
# _detect_pi_output_failure — rate_limited flag
# ---------------------------------------------------------------------------


class TestDetectPiOutputFailureRateLimit:
    def test_auto_retry_rate_limit(self):
        stdout = json.dumps({
            "type": "auto_retry_end",
            "success": False,
            "finalError": '429 {"type":"error","error":{"type":"rate_limit_error","message":"Rate limit exceeded"}}',
        })
        result = _detect_pi_output_failure(stdout)
        assert result is not None
        assert result.rate_limited is True
        assert "rate_limit" in result.error

    def test_auto_retry_overloaded(self):
        stdout = json.dumps({
            "type": "auto_retry_end",
            "success": False,
            "finalError": '529 overloaded_error',
        })
        result = _detect_pi_output_failure(stdout)
        assert result is not None
        assert result.rate_limited is True

    def test_auto_retry_non_rate_limit(self):
        """Real failures (not rate limits) should have rate_limited=False."""
        stdout = json.dumps({
            "type": "auto_retry_end",
            "success": False,
            "finalError": "Some internal server error",
        })
        result = _detect_pi_output_failure(stdout)
        assert result is not None
        assert result.rate_limited is False

    def test_agent_end_rate_limit(self):
        stdout = json.dumps({
            "type": "agent_end",
            "messages": [{
                "role": "assistant",
                "content": [],
                "errorMessage": "429 rate_limit_error",
                "stopReason": "error",
            }],
        })
        result = _detect_pi_output_failure(stdout)
        assert result is not None
        assert result.rate_limited is True

    def test_agent_end_real_failure(self):
        stdout = json.dumps({
            "type": "agent_end",
            "messages": [{
                "role": "assistant",
                "content": [],
                "errorMessage": "tool execution failed",
                "stopReason": "error",
            }],
        })
        result = _detect_pi_output_failure(stdout)
        assert result is not None
        assert result.rate_limited is False

    def test_success_returns_none(self):
        stdout = json.dumps({
            "type": "agent_end",
            "messages": [{
                "role": "assistant",
                "content": [{"type": "text", "text": "Done!"}],
            }],
        })
        assert _detect_pi_output_failure(stdout) is None


# ---------------------------------------------------------------------------
# RateLimitPauser
# ---------------------------------------------------------------------------


class TestRateLimitPauser:
    @pytest.mark.asyncio
    async def test_not_paused_initially(self):
        p = RateLimitPauser(wait_seconds=1)
        assert p.is_paused is False
        assert p.resume_at is None
        assert p.pause_count == 0

    @pytest.mark.asyncio
    async def test_wait_if_paused_returns_immediately_when_not_paused(self):
        p = RateLimitPauser(wait_seconds=1)
        # Should return instantly
        await asyncio.wait_for(p.wait_if_paused(), timeout=0.1)

    @pytest.mark.asyncio
    async def test_pause_and_resume(self):
        """Pause blocks, then resumes after wait_seconds."""
        on_pause = AsyncMock()
        on_resume = AsyncMock()
        p = RateLimitPauser(
            wait_seconds=1,  # 1 second for fast test
            on_pause=on_pause,
            on_resume=on_resume,
        )

        # Pause — this should block until the wait completes
        start = time.monotonic()
        await p.pause("rate limit hit")
        elapsed = time.monotonic() - start

        assert elapsed >= 0.9  # waited ~1 second
        assert p.is_paused is False  # resumed
        assert p.pause_count == 1
        on_pause.assert_called_once()
        on_resume.assert_called_once()

        # Check on_pause was called with (wait_seconds, resume_at)
        args = on_pause.call_args[0]
        assert args[0] == 1  # wait_seconds
        assert isinstance(args[1], datetime)  # resume_at

    @pytest.mark.asyncio
    async def test_idempotent_pause(self):
        """Multiple concurrent pause() calls only trigger one wait."""
        pause_count = 0

        async def on_pause(ws, ra):
            nonlocal pause_count
            pause_count += 1

        p = RateLimitPauser(wait_seconds=1, on_pause=on_pause)

        # Two concurrent pauses
        await asyncio.gather(
            p.pause("first"),
            p.pause("second"),
        )

        assert pause_count == 1
        assert p.pause_count == 1

    @pytest.mark.asyncio
    async def test_gate_blocks_during_pause(self):
        """wait_if_paused() blocks while paused, unblocks after."""
        p = RateLimitPauser(wait_seconds=1)
        unblocked = False

        async def waiter():
            nonlocal unblocked
            await p.wait_if_paused()
            unblocked = True

        # Start the pause (don't await — it blocks for 1s)
        pause_task = asyncio.create_task(p.pause("hit"))

        # Give pause a moment to close the gate
        await asyncio.sleep(0.05)
        assert p.is_paused is True

        # Start a waiter — should be blocked
        waiter_task = asyncio.create_task(waiter())
        await asyncio.sleep(0.05)
        assert unblocked is False  # still blocked

        # Wait for pause to complete
        await pause_task
        await asyncio.sleep(0.05)
        assert unblocked is True  # now unblocked

        waiter_task.cancel()
        try:
            await waiter_task
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_cancel(self):
        """cancel() opens the gate immediately."""
        p = RateLimitPauser(wait_seconds=300)  # 5 minutes

        # Start pause in background
        pause_task = asyncio.create_task(p.pause("hit"))
        await asyncio.sleep(0.05)
        assert p.is_paused is True

        # Cancel
        p.cancel()
        await asyncio.sleep(0.1)

        # Gate should be open now
        assert p.is_paused is False
        await asyncio.wait_for(p.wait_if_paused(), timeout=0.1)

        # Clean up
        try:
            await pause_task
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_multiple_pause_cycles(self):
        """Can pause, resume, then pause again."""
        p = RateLimitPauser(wait_seconds=1)

        await p.pause("first hit")
        assert p.pause_count == 1
        assert p.is_paused is False

        # Second pause cycle
        await p.pause("second hit")
        assert p.pause_count == 2
        assert p.is_paused is False


# ---------------------------------------------------------------------------
# RateLimitAwareRunner
# ---------------------------------------------------------------------------


class _MockRunner:
    """Fake runner that returns pre-configured results."""

    def __init__(self, results: list[RunnerResult]):
        self._results = list(results)
        self._call_count = 0

    async def spawn(self, config: RunnerConfig) -> RunnerResult:
        idx = min(self._call_count, len(self._results) - 1)
        self._call_count += 1
        return self._results[idx]

    def extract_final_output(self, stdout: str) -> str:
        return "output"

    @property
    def call_count(self) -> int:
        return self._call_count


class TestRateLimitAwareRunner:
    @pytest.mark.asyncio
    async def test_success_passes_through(self):
        """Non-rate-limited results pass through without retry."""
        inner = _MockRunner([
            RunnerResult(exit_code=0, stdout="ok", stderr=""),
        ])
        pauser = RateLimitPauser(wait_seconds=1)
        runner = RateLimitAwareRunner(inner, pauser, max_retries=3)

        config = RunnerConfig(task_id="t1", prompt="do stuff", cwd="/tmp")
        result = await runner.spawn(config)

        assert result.exit_code == 0
        assert inner.call_count == 1
        assert pauser.pause_count == 0

    @pytest.mark.asyncio
    async def test_real_failure_passes_through(self):
        """Non-rate-limit failures pass through without retry."""
        inner = _MockRunner([
            RunnerResult(exit_code=1, stdout="", stderr="TypeError: blah"),
        ])
        pauser = RateLimitPauser(wait_seconds=1)
        runner = RateLimitAwareRunner(inner, pauser, max_retries=3)

        config = RunnerConfig(task_id="t1", prompt="do stuff", cwd="/tmp")
        result = await runner.spawn(config)

        assert result.exit_code == 1
        assert inner.call_count == 1
        assert pauser.pause_count == 0

    @pytest.mark.asyncio
    async def test_rate_limit_then_success(self):
        """Rate limit → pause → retry → success."""
        inner = _MockRunner([
            RunnerResult(exit_code=1, stdout="", stderr="429 rate_limit_error", rate_limited=True),
            RunnerResult(exit_code=0, stdout="done", stderr=""),
        ])
        pauser = RateLimitPauser(wait_seconds=1)  # 1s for fast test
        runner = RateLimitAwareRunner(inner, pauser, max_retries=3)

        config = RunnerConfig(task_id="t1", prompt="do stuff", cwd="/tmp")

        start = time.monotonic()
        result = await runner.spawn(config)
        elapsed = time.monotonic() - start

        assert result.exit_code == 0
        assert inner.call_count == 2
        assert pauser.pause_count == 1
        assert elapsed >= 0.9  # waited for pause

    @pytest.mark.asyncio
    async def test_exhausts_retries(self):
        """All retries rate-limited → returns last failure."""
        rate_limited = RunnerResult(
            exit_code=1, stdout="", stderr="429 rate_limit_error", rate_limited=True
        )
        inner = _MockRunner([rate_limited, rate_limited, rate_limited, rate_limited])
        pauser = RateLimitPauser(wait_seconds=1)
        runner = RateLimitAwareRunner(inner, pauser, max_retries=2)

        config = RunnerConfig(task_id="t1", prompt="do stuff", cwd="/tmp")
        result = await runner.spawn(config)

        assert result.exit_code == 1
        assert result.rate_limited is True
        assert inner.call_count == 3  # initial + 2 retries
        assert pauser.pause_count == 2  # paused twice (not on final attempt)

    @pytest.mark.asyncio
    async def test_extract_final_output_delegates(self):
        inner = _MockRunner([])
        pauser = RateLimitPauser(wait_seconds=1)
        runner = RateLimitAwareRunner(inner, pauser)

        assert runner.extract_final_output("anything") == "output"

    @pytest.mark.asyncio
    async def test_concurrent_tasks_share_pause(self):
        """Two tasks hit rate limit concurrently — second one waits on the
        existing pause instead of starting its own."""
        pause_entered = asyncio.Event()
        spawn_log: list[str] = []

        class _TrackingRunner:
            async def spawn(self, config):
                spawn_log.append(config.task_id)
                if len(spawn_log) <= 2:
                    # Both initial calls hit rate limit
                    return RunnerResult(
                        exit_code=1, stdout="", stderr="429", rate_limited=True
                    )
                return RunnerResult(exit_code=0, stdout="ok", stderr="")

            def extract_final_output(self, stdout):
                return "ok"

        pauser = RateLimitPauser(wait_seconds=1)
        runner = RateLimitAwareRunner(_TrackingRunner(), pauser, max_retries=3)

        config1 = RunnerConfig(task_id="t1", prompt="a", cwd="/tmp")
        config2 = RunnerConfig(task_id="t2", prompt="b", cwd="/tmp")

        results = await asyncio.gather(
            runner.spawn(config1),
            runner.spawn(config2),
        )

        # Both should eventually succeed after retries
        assert all(r.exit_code == 0 for r in results)

    @pytest.mark.asyncio
    async def test_pattern_detection_without_flag(self):
        """Rate limit detected via stderr patterns even without flag."""
        inner = _MockRunner([
            RunnerResult(exit_code=1, stdout="", stderr="overloaded_error: service busy"),
            RunnerResult(exit_code=0, stdout="done", stderr=""),
        ])
        pauser = RateLimitPauser(wait_seconds=1)
        runner = RateLimitAwareRunner(inner, pauser, max_retries=3)

        config = RunnerConfig(task_id="t1", prompt="do stuff", cwd="/tmp")
        result = await runner.spawn(config)

        assert result.exit_code == 0
        assert inner.call_count == 2
        assert pauser.pause_count == 1


# ---------------------------------------------------------------------------
# Integration: PiRunner rate_limited flag on RunnerResult
# ---------------------------------------------------------------------------


class TestPiRunnerRateLimitedFlag:
    """Verify PiRunner.spawn() sets rate_limited on the RunnerResult."""

    @pytest.mark.asyncio
    async def test_rate_limited_output_sets_flag(self):
        from unittest.mock import patch, AsyncMock

        from wave_server.engine.runner import PiRunner

        rate_limited_output = "\n".join([
            json.dumps({
                "type": "agent_end",
                "messages": [{
                    "role": "assistant",
                    "content": [],
                    "errorMessage": "429 rate_limit_error",
                    "stopReason": "error",
                }],
            }),
            json.dumps({
                "type": "auto_retry_end",
                "success": False,
                "finalError": "429 rate_limit_error",
            }),
        ])

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(
            return_value=(rate_limited_output.encode(), b"")
        )
        mock_proc.returncode = 0

        runner = PiRunner()

        with patch("shutil.which", return_value="/usr/bin/pi"), \
             patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            config = RunnerConfig(task_id="t1", prompt="test", cwd="/tmp")
            result = await runner.spawn(config)

        assert result.exit_code == 1
        assert result.rate_limited is True

    @pytest.mark.asyncio
    async def test_real_failure_does_not_set_flag(self):
        from unittest.mock import patch, AsyncMock

        from wave_server.engine.runner import PiRunner

        failure_output = json.dumps({
            "type": "agent_end",
            "messages": [{
                "role": "assistant",
                "content": [],
                "errorMessage": "tool bash failed: command not found",
                "stopReason": "error",
            }],
        })

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(
            return_value=(failure_output.encode(), b"")
        )
        mock_proc.returncode = 0

        runner = PiRunner()

        with patch("shutil.which", return_value="/usr/bin/pi"), \
             patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            config = RunnerConfig(task_id="t1", prompt="test", cwd="/tmp")
            result = await runner.spawn(config)

        assert result.exit_code == 1
        assert result.rate_limited is False

    @pytest.mark.asyncio
    async def test_successful_run_not_rate_limited(self):
        from unittest.mock import patch, AsyncMock

        from wave_server.engine.runner import PiRunner

        success_output = json.dumps({
            "type": "agent_end",
            "messages": [{
                "role": "assistant",
                "content": [{"type": "text", "text": "All done!"}],
            }],
        })

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(
            return_value=(success_output.encode(), b"")
        )
        mock_proc.returncode = 0

        runner = PiRunner()

        with patch("shutil.which", return_value="/usr/bin/pi"), \
             patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            config = RunnerConfig(task_id="t1", prompt="test", cwd="/tmp")
            result = await runner.spawn(config)

        assert result.exit_code == 0
        assert result.rate_limited is False
