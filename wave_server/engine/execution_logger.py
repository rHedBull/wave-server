"""Structured execution-level logger.

Produces a human-readable Markdown log for the entire execution run:
header with metadata, per-wave/phase/task entries with timestamps,
and a final summary with pass/fail counts, duration, and cost totals.

Complements the per-task agent logs (log_parser.py) with the
orchestrator's view of the run.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from wave_server.engine.types import Task, TaskResult


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _elapsed_str(start: float) -> str:
    """Format elapsed time since `start` (monotonic) as [MM:SS]."""
    s = int(time.monotonic() - start)
    m, sec = divmod(s, 60)
    return f"[{m:02d}:{sec:02d}]"


def _duration_str(ms: int) -> str:
    if ms < 1000:
        return f"{ms}ms"
    s = ms / 1000
    if s < 60:
        return f"{s:.1f}s"
    m = int(s // 60)
    sec = int(s % 60)
    return f"{m}m {sec:02d}s"


def _status_icon(exit_code: int, timed_out: bool) -> str:
    if timed_out:
        return "⏰"
    if exit_code == 0:
        return "✅"
    if exit_code == -1:
        return "⏭️"
    return "❌"


def _agent_icon(agent: str) -> str:
    if agent == "test-writer":
        return "🧪"
    if agent == "wave-verifier":
        return "🔍"
    return "🔨"


@dataclass
class TaskRecord:
    """Outcome record for a single task."""

    task_id: str
    title: str
    agent: str
    phase: str
    exit_code: int
    duration_ms: int
    timed_out: bool
    error_snippet: str = ""


@dataclass
class WaveRecord:
    """Outcome record for a single wave."""

    name: str
    index: int
    passed: bool
    tasks: list[TaskRecord] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""


@dataclass
class ExecutionLogger:
    """Accumulates execution events and renders them as a Markdown log.

    Usage::

        logger = ExecutionLogger(...)
        logger.execution_started()
        logger.wave_started("Wave 1: Setup", 0)
        logger.phase_started("foundation")
        logger.task_started("foundation", task)
        logger.task_ended("foundation", task, result)
        ...
        logger.wave_ended("Wave 1: Setup", 0, passed=True)
        logger.execution_finished(all_passed=True)
        text = logger.render()
    """

    execution_id: str
    runtime: str = "pi"
    total_tasks: int = 0
    max_concurrency: int = 4
    goal: str = ""
    wave_count: int = 0
    spec_path: str = ""
    plan_path: str = ""

    # Internal state
    _lines: list[str] = field(default_factory=list)
    _start_time: float = field(default_factory=time.monotonic)
    _waves: list[WaveRecord] = field(default_factory=list)
    _current_wave: WaveRecord | None = field(default=None, repr=False)
    _task_start_times: dict[str, float] = field(default_factory=dict)
    _total_cost: float = 0.0
    _total_input_tokens: int = 0
    _total_output_tokens: int = 0

    # ── Lifecycle Events ──────────────────────────────────────

    def execution_started(self) -> None:
        self._start_time = time.monotonic()
        self._log("# Execution Log")
        self._log("")
        self._log(f"Started: {_now_iso()}")
        self._log(f"Execution ID: `{self.execution_id}`")
        self._log(f"Runtime: {self.runtime}")
        self._log(f"Concurrency: {self.max_concurrency}")
        if self.goal:
            self._log(f"Goal: {self.goal}")
        self._log(f"Plan: {self.wave_count} waves, {self.total_tasks} tasks")
        self._log("")

    def execution_finished(self, *, all_passed: bool) -> None:
        self._log("")
        self._log("---")
        self._log("")

        total_ms = int((time.monotonic() - self._start_time) * 1000)

        # Count outcomes
        all_tasks = [t for w in self._waves for t in w.tasks]
        passed = sum(1 for t in all_tasks if t.exit_code == 0)
        failed = sum(1 for t in all_tasks if t.exit_code > 0)
        skipped = sum(1 for t in all_tasks if t.exit_code == -1)
        timed_out = sum(1 for t in all_tasks if t.timed_out)

        status = "SUCCESS" if all_passed else "FAILED"
        icon = "✅" if all_passed else "❌"

        self._log(f"# {icon} {status}")
        self._log("")
        self._log(f"Finished: {_now_iso()}")
        self._log(f"Duration: {_duration_str(total_ms)}")
        self._log(
            f"Tasks: {passed} passed, {failed} failed, {skipped} skipped ({passed + failed + skipped}/{self.total_tasks})"
        )
        if timed_out:
            self._log(f"Timed out: {timed_out}")
        self._log(
            f"Waves: {len(self._waves)}/{self.wave_count}"
            + (
                " (stopped at failure)"
                if not all_passed and len(self._waves) < self.wave_count
                else ""
            )
        )

        if self._total_cost > 0:
            self._log(f"Total cost: ${self._total_cost:.4f}")
        if self._total_input_tokens or self._total_output_tokens:
            self._log(
                f"Total tokens: {self._total_input_tokens:,} in, {self._total_output_tokens:,} out"
            )

        # Per-wave summary
        self._log("")
        for w in self._waves:
            w_passed = sum(1 for t in w.tasks if t.exit_code == 0)
            w_icon = "✅" if w.passed else "❌"
            self._log(f"{w_icon} **{w.name}**: {w_passed}/{len(w.tasks)} tasks passed")

        # Failed task details
        failed_tasks = [t for t in all_tasks if t.exit_code > 0]
        if failed_tasks:
            self._log("")
            self._log("### Failed Tasks")
            self._log("")
            for t in failed_tasks:
                self._log(
                    f"- **{t.task_id}** [{t.agent}]: {t.title} ({_duration_str(t.duration_ms)})"
                )
                if t.error_snippet:
                    self._log(f"  > {t.error_snippet}")

    # ── Wave Events ───────────────────────────────────────────

    def wave_started(self, name: str, index: int) -> None:
        self._current_wave = WaveRecord(
            name=name, index=index, passed=False, started_at=_now_iso()
        )
        self._log(f"## {_elapsed_str(self._start_time)} Wave {index + 1}: {name}")
        self._log("")

    def wave_ended(self, name: str, index: int, *, passed: bool) -> None:
        if self._current_wave:
            self._current_wave.passed = passed
            self._current_wave.finished_at = _now_iso()
            self._waves.append(self._current_wave)

        icon = "✅" if passed else "❌"
        task_count = len(self._current_wave.tasks) if self._current_wave else 0
        passed_count = sum(
            1
            for t in (self._current_wave.tasks if self._current_wave else [])
            if t.exit_code == 0
        )
        self._log("")
        self._log(
            f"{_elapsed_str(self._start_time)} {icon} Wave {index + 1} {'passed' if passed else 'FAILED'} — {passed_count}/{task_count} tasks"
        )
        self._log("")
        self._current_wave = None

    # ── Phase Events ──────────────────────────────────────────

    def phase_started(self, phase: str) -> None:
        self._log(f"### {_elapsed_str(self._start_time)} {phase.title()}")
        self._log("")

    def phase_skipped(self, phase: str, reason: str) -> None:
        self._log(
            f"{_elapsed_str(self._start_time)} ⏭️ {phase.title()} skipped — {reason}"
        )
        self._log("")

    # ── Task Events ───────────────────────────────────────────

    def task_started(self, phase: str, task: Task) -> None:
        self._task_start_times[task.id] = time.monotonic()
        self._log(
            f"{_elapsed_str(self._start_time)} ▶ {_agent_icon(task.agent)} **{task.id}** [{task.agent}]: {task.title}"
        )

    def task_ended(self, phase: str, task: Task, result: TaskResult) -> None:
        icon = _status_icon(result.exit_code, result.timed_out)
        suffix = " **TIMED OUT**" if result.timed_out else ""
        self._log(
            f"{_elapsed_str(self._start_time)} {icon} {_agent_icon(task.agent)} "
            f"**{task.id}** [{task.agent}]: {task.title} "
            f"({_duration_str(result.duration_ms)}){suffix}"
        )

        # Log brief error for failed tasks
        error_snippet = ""
        if result.exit_code > 0 and result.stderr:
            err_lines = [
                line.strip() for line in result.stderr.split("\n") if line.strip()
            ][:3]
            if err_lines:
                error_snippet = err_lines[0][:200]
                for line in err_lines:
                    self._log(f"    > {line[:200]}")

        # Track in current wave
        if self._current_wave is not None:
            self._current_wave.tasks.append(
                TaskRecord(
                    task_id=task.id,
                    title=task.title,
                    agent=task.agent,
                    phase=phase,
                    exit_code=result.exit_code,
                    duration_ms=result.duration_ms,
                    timed_out=result.timed_out,
                    error_snippet=error_snippet,
                )
            )

        # Accumulate cost/tokens from parsed log if available
        self._task_start_times.pop(task.id, None)

    def add_cost(
        self, cost_usd: float, input_tokens: int = 0, output_tokens: int = 0
    ) -> None:
        """Add cost/token data (called after parsing the task's stream-json)."""
        self._total_cost += cost_usd
        self._total_input_tokens += input_tokens
        self._total_output_tokens += output_tokens

    # ── Feature Events ────────────────────────────────────────

    def feature_started(self, name: str) -> None:
        if name != "default":
            self._log(f"{_elapsed_str(self._start_time)} #### Feature: {name}")

    def feature_ended(self, name: str, passed: bool) -> None:
        if name != "default":
            icon = "✅" if passed else "❌"
            self._log(
                f"{_elapsed_str(self._start_time)} {icon} Feature '{name}' {'passed' if passed else 'FAILED'}"
            )
            self._log("")

    # ── Misc Events ───────────────────────────────────────────

    def log(self, line: str) -> None:
        """Append a free-form log line with timestamp."""
        self._log(f"{_elapsed_str(self._start_time)} {line}")

    def log_raw(self, line: str) -> None:
        """Append a raw line without timestamp."""
        self._log(line)

    # ── Rendering ─────────────────────────────────────────────

    def render(self) -> str:
        """Render the complete log as Markdown."""
        return "\n".join(self._lines) + "\n"

    def render_lines(self) -> list[str]:
        """Return a copy of the log lines."""
        return list(self._lines)

    # ── Internal ──────────────────────────────────────────────

    def _log(self, line: str) -> None:
        self._lines.append(line)
