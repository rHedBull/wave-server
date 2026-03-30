"""Tests for the verify-fix loop."""

import pytest

from wave_server.engine.verify_fix import attempt_fix_and_reverify, _build_fix_prompt
from wave_server.engine.types import Task, RunnerConfig, RunnerResult


# ── Mock runner ────────────────────────────────────────────────


class MockRunner:
    """Configurable mock runner for testing fix-verify loops."""

    def __init__(self, responses: list[RunnerResult]):
        self.responses = list(responses)
        self.calls: list[RunnerConfig] = []

    async def spawn(self, config: RunnerConfig) -> RunnerResult:
        self.calls.append(config)
        if self.responses:
            return self.responses.pop(0)
        return RunnerResult(
            exit_code=1, stdout="", stderr="no more responses", timed_out=False
        )

    def extract_final_output(self, stdout: str) -> str:
        return stdout


def _make_task(**kwargs) -> Task:
    defaults = {
        "id": "w1-t3",
        "title": "Verify feature X",
        "agent": "wave-verifier",
        "depends": [],
        "files": ["src/feature.py", "tests/test_feature.py"],
        "description": "Verify feature X implementation",
        "test_files": [],
    }
    defaults.update(kwargs)
    return Task(**defaults)


FAIL_OUTPUT = '{"status": "fail", "summary": "Test fails", "issues": [{"severity": "error", "description": "await on sync method", "file": "tests/test_feature.py", "suggestion": "Remove await"}], "readyForNextWave": false}'
PASS_OUTPUT = '{"status": "pass", "summary": "All good", "readyForNextWave": true}'


# ── _build_fix_prompt ──────────────────────────────────────────


class TestBuildFixPrompt:
    def test_includes_verifier_output(self):
        task = _make_task()
        prompt = _build_fix_prompt(FAIL_OUTPUT, task)
        assert "await on sync method" in prompt
        assert "Remove await" in prompt

    def test_includes_task_context(self):
        task = _make_task(id="w2-t3", title="Verify bugs")
        prompt = _build_fix_prompt(FAIL_OUTPUT, task)
        assert "w2-t3" in prompt
        assert "Verify bugs" in prompt

    def test_includes_files(self):
        task = _make_task(files=["src/router.py", "src/schema.py"])
        prompt = _build_fix_prompt(FAIL_OUTPUT, task)
        assert "src/router.py" in prompt
        assert "src/schema.py" in prompt

    def test_surgical_instruction(self):
        task = _make_task()
        prompt = _build_fix_prompt(FAIL_OUTPUT, task)
        assert "surgical" in prompt.lower() or "ONLY the fixes" in prompt


# ── attempt_fix_and_reverify ───────────────────────────────────


class TestAttemptFixAndReverify:
    @pytest.mark.asyncio
    async def test_fix_succeeds_first_attempt(self):
        """Fix agent runs, verifier passes on first re-check."""
        runner = MockRunner(
            [
                # Fix agent succeeds
                RunnerResult(exit_code=0, stdout="Fixed!", stderr="", timed_out=False),
                # Re-verification passes
                RunnerResult(
                    exit_code=0, stdout=PASS_OUTPUT, stderr="", timed_out=False
                ),
            ]
        )
        task = _make_task()

        result = await attempt_fix_and_reverify(
            verifier_task=task,
            verifier_output=FAIL_OUTPUT,
            verifier_prompt="verify stuff",
            runner=runner,
            cwd="/tmp",
            max_attempts=2,
        )

        assert result is not None
        assert result.exit_code == 0
        assert result.id == task.id
        assert len(runner.calls) == 2  # fix + verify

    @pytest.mark.asyncio
    async def test_fix_succeeds_second_attempt(self):
        """First fix doesn't fully work, second attempt passes."""
        runner = MockRunner(
            [
                # Attempt 1: fix agent succeeds
                RunnerResult(
                    exit_code=0, stdout="partial fix", stderr="", timed_out=False
                ),
                # Attempt 1: re-verify still fails
                RunnerResult(
                    exit_code=0, stdout=FAIL_OUTPUT, stderr="", timed_out=False
                ),
                # Attempt 2: fix agent succeeds
                RunnerResult(
                    exit_code=0, stdout="full fix", stderr="", timed_out=False
                ),
                # Attempt 2: re-verify passes
                RunnerResult(
                    exit_code=0, stdout=PASS_OUTPUT, stderr="", timed_out=False
                ),
            ]
        )
        task = _make_task()

        result = await attempt_fix_and_reverify(
            verifier_task=task,
            verifier_output=FAIL_OUTPUT,
            verifier_prompt="verify stuff",
            runner=runner,
            cwd="/tmp",
            max_attempts=2,
        )

        assert result is not None
        assert result.exit_code == 0
        assert len(runner.calls) == 4  # fix + verify + fix + verify

    @pytest.mark.asyncio
    async def test_all_attempts_exhausted(self):
        """Fix agent runs but verifier keeps failing."""
        runner = MockRunner(
            [
                # Attempt 1: fix succeeds, verify fails
                RunnerResult(exit_code=0, stdout="fix1", stderr="", timed_out=False),
                RunnerResult(
                    exit_code=0, stdout=FAIL_OUTPUT, stderr="", timed_out=False
                ),
                # Attempt 2: fix succeeds, verify fails
                RunnerResult(exit_code=0, stdout="fix2", stderr="", timed_out=False),
                RunnerResult(
                    exit_code=0, stdout=FAIL_OUTPUT, stderr="", timed_out=False
                ),
            ]
        )
        task = _make_task()

        result = await attempt_fix_and_reverify(
            verifier_task=task,
            verifier_output=FAIL_OUTPUT,
            verifier_prompt="verify stuff",
            runner=runner,
            cwd="/tmp",
            max_attempts=2,
        )

        assert result is None
        assert len(runner.calls) == 4

    @pytest.mark.asyncio
    async def test_fix_agent_fails(self):
        """Fix agent itself crashes — skip to next attempt."""
        runner = MockRunner(
            [
                # Attempt 1: fix agent crashes
                RunnerResult(exit_code=1, stdout="", stderr="crash", timed_out=False),
                # Attempt 2: fix agent succeeds, verify passes
                RunnerResult(exit_code=0, stdout="fixed", stderr="", timed_out=False),
                RunnerResult(
                    exit_code=0, stdout=PASS_OUTPUT, stderr="", timed_out=False
                ),
            ]
        )
        task = _make_task()

        result = await attempt_fix_and_reverify(
            verifier_task=task,
            verifier_output=FAIL_OUTPUT,
            verifier_prompt="verify stuff",
            runner=runner,
            cwd="/tmp",
            max_attempts=2,
        )

        assert result is not None
        assert result.exit_code == 0
        assert len(runner.calls) == 3  # failed fix + successful fix + verify

    @pytest.mark.asyncio
    async def test_reverify_nonzero_exit(self):
        """Re-verification process crashes (non-zero exit) — counts as failure."""
        runner = MockRunner(
            [
                # Fix succeeds
                RunnerResult(exit_code=0, stdout="fixed", stderr="", timed_out=False),
                # Verify crashes
                RunnerResult(exit_code=1, stdout="", stderr="crash", timed_out=False),
            ]
        )
        task = _make_task()

        result = await attempt_fix_and_reverify(
            verifier_task=task,
            verifier_output=FAIL_OUTPUT,
            verifier_prompt="verify stuff",
            runner=runner,
            cwd="/tmp",
            max_attempts=1,
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_max_attempts_one(self):
        """Single attempt — one fix, one verify."""
        runner = MockRunner(
            [
                RunnerResult(exit_code=0, stdout="fixed", stderr="", timed_out=False),
                RunnerResult(
                    exit_code=0, stdout=PASS_OUTPUT, stderr="", timed_out=False
                ),
            ]
        )
        task = _make_task()

        result = await attempt_fix_and_reverify(
            verifier_task=task,
            verifier_output=FAIL_OUTPUT,
            verifier_prompt="verify stuff",
            runner=runner,
            cwd="/tmp",
            max_attempts=1,
        )

        assert result is not None
        assert len(runner.calls) == 2

    @pytest.mark.asyncio
    async def test_uses_correct_models(self):
        """Fix agent uses worker model, verifier uses wave-verifier model."""
        runner = MockRunner(
            [
                RunnerResult(exit_code=0, stdout="fixed", stderr="", timed_out=False),
                RunnerResult(
                    exit_code=0, stdout=PASS_OUTPUT, stderr="", timed_out=False
                ),
            ]
        )
        task = _make_task()

        await attempt_fix_and_reverify(
            verifier_task=task,
            verifier_output=FAIL_OUTPUT,
            verifier_prompt="verify stuff",
            runner=runner,
            cwd="/tmp",
            model="default-model",
            agent_models={"worker": "worker-model", "wave-verifier": "verifier-model"},
            max_attempts=1,
        )

        assert len(runner.calls) == 2
        # Fix agent should use worker model
        assert runner.calls[0].model == "worker-model"
        # Re-verification should use verifier model
        assert runner.calls[1].model == "verifier-model"

    @pytest.mark.asyncio
    async def test_logs_emitted(self):
        """on_log callback is called at each step."""
        logs: list[str] = []

        runner = MockRunner(
            [
                RunnerResult(exit_code=0, stdout="fixed", stderr="", timed_out=False),
                RunnerResult(
                    exit_code=0, stdout=PASS_OUTPUT, stderr="", timed_out=False
                ),
            ]
        )
        task = _make_task()

        await attempt_fix_and_reverify(
            verifier_task=task,
            verifier_output=FAIL_OUTPUT,
            verifier_prompt="verify stuff",
            runner=runner,
            cwd="/tmp",
            max_attempts=1,
            on_log=lambda msg: logs.append(msg),
        )

        assert any("Fix attempt" in log for log in logs)
        assert any("Fix agent completed" in log or "✅" in log for log in logs)
        assert any("Re-verifying" in log or "🔍" in log for log in logs)
        assert any("passed" in log.lower() or "✅" in log for log in logs)

    @pytest.mark.asyncio
    async def test_verifier_prompt_passed_to_reverify(self):
        """The original verifier prompt is reused for re-verification."""
        runner = MockRunner(
            [
                RunnerResult(exit_code=0, stdout="fixed", stderr="", timed_out=False),
                RunnerResult(
                    exit_code=0, stdout=PASS_OUTPUT, stderr="", timed_out=False
                ),
            ]
        )
        task = _make_task()
        original_prompt = "You are verifying: check files exist, run tests"

        await attempt_fix_and_reverify(
            verifier_task=task,
            verifier_output=FAIL_OUTPUT,
            verifier_prompt=original_prompt,
            runner=runner,
            cwd="/tmp",
            max_attempts=1,
        )

        # Second call is the re-verification — should use the original prompt
        assert runner.calls[1].prompt == original_prompt

    @pytest.mark.asyncio
    async def test_updated_output_on_second_attempt(self):
        """Second fix attempt gets the updated verifier output from the failed re-verify."""
        second_fail = '{"status": "fail", "summary": "New issue found", "readyForNextWave": false}'

        runner = MockRunner(
            [
                # Attempt 1: fix, re-verify still fails with different message
                RunnerResult(exit_code=0, stdout="fix1", stderr="", timed_out=False),
                RunnerResult(
                    exit_code=0, stdout=second_fail, stderr="", timed_out=False
                ),
                # Attempt 2: fix with updated feedback, re-verify passes
                RunnerResult(exit_code=0, stdout="fix2", stderr="", timed_out=False),
                RunnerResult(
                    exit_code=0, stdout=PASS_OUTPUT, stderr="", timed_out=False
                ),
            ]
        )
        task = _make_task()

        result = await attempt_fix_and_reverify(
            verifier_task=task,
            verifier_output=FAIL_OUTPUT,
            verifier_prompt="verify stuff",
            runner=runner,
            cwd="/tmp",
            max_attempts=2,
        )

        assert result is not None
        # The second fix prompt should contain the UPDATED failure message
        second_fix_prompt = runner.calls[2].prompt
        assert "New issue found" in second_fix_prompt
