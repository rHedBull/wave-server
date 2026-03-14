"""Verify-fix loop — when a verifier detects issues, spawn a fix agent and re-verify.

Flow:
  1. Verifier reports failure with specific issues
  2. A fix agent is spawned with the verifier's feedback
  3. Fix agent applies surgical fixes and commits
  4. Verifier re-runs to confirm the fix and check for regressions
  5. If still failing, loop up to max_attempts times

This avoids failing an entire execution over trivial, well-identified bugs
that a verifier has already diagnosed.
"""

from __future__ import annotations

import inspect
import time
from collections.abc import Callable
from typing import Any

from wave_server.engine.enforcement import is_verifier_failure
from wave_server.engine.git_worktree import commit_task_output
from wave_server.engine.runner import AgentRunner
from wave_server.engine.types import RunnerConfig, Task, TaskResult


async def _call(fn: Callable | None, *args: Any) -> None:
    if fn is None:
        return
    result = fn(*args)
    if inspect.isawaitable(result):
        await result


def _build_fix_prompt(verifier_output: str, task: Task) -> str:
    """Build a prompt for the fix agent based on the verifier's failure report."""
    files_line = f"Files: {', '.join(task.files)}" if task.files else ""

    return f"""You are a surgical code fixer. A verification step found issues that need to be fixed.

## Verifier Report

{verifier_output}

## Context

The verifier checked work for task **{task.id}: {task.title}**.
{files_line}

## Instructions

1. Read the verifier report carefully — it lists exact files, lines, and suggestions
2. Apply ONLY the fixes described — do not refactor, reorganize, or add unrelated changes
3. After fixing, run the relevant tests to confirm your fix works
4. If the verifier mentions missing files, check if they exist under a different name or path before creating new ones

Be surgical. Fix exactly what the verifier flagged, nothing more.

- Work continuously — do NOT stop to summarize progress or wait for feedback"""


async def attempt_fix_and_reverify(
    verifier_task: Task,
    verifier_output: str,
    verifier_prompt: str,
    runner: AgentRunner,
    cwd: str,
    env: dict[str, str] | None = None,
    model: str | None = None,
    agent_models: dict[str, str] | None = None,
    max_attempts: int = 2,
    auto_commit: bool = False,
    on_log: Callable[[str], Any] | None = None,
) -> TaskResult | None:
    """Attempt to fix verifier-detected issues and re-verify.

    Spawns a fix agent with the verifier's feedback, then re-runs the
    verifier.  Loops up to *max_attempts* times.

    Returns a successful ``TaskResult`` (exit_code 0) if the fix-verify
    loop succeeds, or ``None`` if all attempts are exhausted.
    """
    current_output = verifier_output

    for attempt in range(1, max_attempts + 1):
        await _call(on_log, f"   🔧 Fix attempt {attempt}/{max_attempts} for {verifier_task.id}")

        # ── 1. Run fix agent ──────────────────────────────────
        fix_prompt = _build_fix_prompt(current_output, verifier_task)

        # Use worker model for the fix agent
        fix_model = (agent_models or {}).get("worker") or model

        fix_config = RunnerConfig(
            task_id=f"{verifier_task.id}-fix-{attempt}",
            prompt=fix_prompt,
            cwd=cwd,
            env=env,
            model=fix_model,
        )

        fix_start = time.monotonic()
        fix_result = await runner.spawn(fix_config)
        fix_elapsed = int((time.monotonic() - fix_start) * 1000)

        if fix_result.exit_code != 0:
            await _call(
                on_log,
                f"   ❌ Fix agent failed (exit {fix_result.exit_code}, {fix_elapsed}ms)",
            )
            continue

        await _call(on_log, f"   ✅ Fix agent completed ({fix_elapsed}ms)")

        # ── 2. Commit fix ─────────────────────────────────────
        if auto_commit:
            committed = await commit_task_output(
                cwd,
                f"{verifier_task.id}-fix-{attempt}",
                f"Fix attempt {attempt} for {verifier_task.title}",
                "worker",
            )
            if committed:
                await _call(on_log, f"   📌 Committed fix attempt {attempt}")

        # ── 3. Re-run verifier ────────────────────────────────
        await _call(on_log, f"   🔍 Re-verifying {verifier_task.id}...")

        verify_model = (agent_models or {}).get("wave-verifier") or model

        verify_config = RunnerConfig(
            task_id=f"{verifier_task.id}-reverify-{attempt}",
            prompt=verifier_prompt,
            cwd=cwd,
            env=env,
            model=verify_model,
        )

        verify_start = time.monotonic()
        verify_result = await runner.spawn(verify_config)
        verify_elapsed = int((time.monotonic() - verify_start) * 1000)
        verify_output = runner.extract_final_output(verify_result.stdout)

        if verify_result.exit_code != 0:
            await _call(
                on_log,
                f"   ❌ Re-verification failed (exit {verify_result.exit_code}, {verify_elapsed}ms)",
            )
            current_output = verify_output
            continue

        # Check semantic failure
        if is_verifier_failure(verify_output):
            await _call(
                on_log,
                f"   ❌ Re-verification found remaining issues ({verify_elapsed}ms)",
            )
            current_output = verify_output
            continue

        # ── 4. Success! ───────────────────────────────────────
        await _call(
            on_log,
            f"   ✅ Verification passed after fix attempt {attempt} ({verify_elapsed}ms)",
        )

        total_elapsed = fix_elapsed + verify_elapsed
        return TaskResult(
            id=verifier_task.id,
            title=verifier_task.title,
            agent=verifier_task.agent,
            exit_code=0,
            output=verify_output,
            stderr="",
            duration_ms=total_elapsed,
            stdout=verify_result.stdout,
            timed_out=False,
        )

    # All attempts exhausted
    await _call(
        on_log,
        f"   ❌ All {max_attempts} fix attempts exhausted for {verifier_task.id}",
    )
    return None
