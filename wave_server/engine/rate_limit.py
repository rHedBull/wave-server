"""Rate-limit pause-and-resume for the execution engine.

When a task hits an API rate limit (e.g. Claude Code subscription's 4–5 h
window), the execution pauses, waits for the window to reset, then
automatically resumes where it left off.

Architecture
~~~~~~~~~~~~
- **RateLimitPauser** — shared coordinator.  Holds an ``asyncio.Event``
  gate that blocks all new task spawns while paused.  Only the first
  rate-limit report triggers the wait; subsequent reports are idempotent.

- **RateLimitAwareRunner** — drop-in ``AgentRunner`` wrapper.  Before
  every spawn it checks the gate, after every spawn it inspects the
  result, and on rate-limit it triggers the pauser + retries.

Integration is transparent to the DAG / wave / feature executors —
they receive a wrapped runner and never see rate-limit failures.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from wave_server.engine.runner import AgentRunner
from wave_server.engine.types import RunnerConfig, RunnerResult

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------

_RATE_LIMIT_PATTERNS = [
    "rate_limit",
    "rate limit",
    "429",
    "overloaded",
    "overloaded_error",
    "529",
    "too many requests",
]


def is_rate_limit_error(result: RunnerResult) -> bool:
    """Check whether a runner result indicates an API rate limit.

    Uses the explicit ``rate_limited`` flag first, then falls back to
    pattern-matching on stderr/stdout for robustness.
    """
    if result.rate_limited:
        return True
    if result.exit_code == 0:
        return False
    combined = f"{result.stderr} {result.stdout}".lower()
    return any(p in combined for p in _RATE_LIMIT_PATTERNS)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _call(fn: Callable | None, *args: Any) -> None:
    if fn is None:
        return
    result = fn(*args)
    if inspect.isawaitable(result):
        await result


# ---------------------------------------------------------------------------
# Pauser
# ---------------------------------------------------------------------------


class RateLimitPauser:
    """Coordinates execution pausing when API rate limits are hit.

    Thread-safe via asyncio primitives.  Multiple concurrent tasks may
    report rate limits; only the first triggers the sleep — all others
    block on :meth:`wait_if_paused` until the window resets.

    Parameters
    ----------
    wait_seconds:
        How long to sleep when a rate limit is hit (default 5 h).
    on_pause:
        ``async def on_pause(wait_seconds: int, resume_at: datetime)``
        — called once when the pause begins.
    on_resume:
        ``async def on_resume()`` — called once when the pause ends.
    """

    def __init__(
        self,
        wait_seconds: int = 18000,
        on_pause: Callable[[int, datetime], Any] | None = None,
        on_resume: Callable[[], Any] | None = None,
    ):
        self._wait_seconds = wait_seconds
        self._on_pause = on_pause
        self._on_resume = on_resume

        self._gate = asyncio.Event()
        self._gate.set()  # open — not paused
        self._paused = False
        self._resume_at: datetime | None = None
        self._pause_count = 0
        self._wait_task: asyncio.Task | None = None

    # -- Public properties --------------------------------------------------

    @property
    def is_paused(self) -> bool:
        return self._paused

    @property
    def resume_at(self) -> datetime | None:
        return self._resume_at

    @property
    def pause_count(self) -> int:
        return self._pause_count

    # -- Gate ---------------------------------------------------------------

    async def wait_if_paused(self) -> None:
        """Block the caller until the rate-limit pause clears.

        Call this before spawning a task.  Returns immediately when not
        paused.
        """
        await self._gate.wait()

    # -- Trigger ------------------------------------------------------------

    async def pause(self, error_msg: str = "") -> None:
        """Enter paused state.  Idempotent — only the first call sleeps."""
        if self._paused:
            # Already paused — just wait for the existing pause to clear
            # so the caller can retry after
            await self._gate.wait()
            return

        self._paused = True
        self._pause_count += 1
        self._gate.clear()

        self._resume_at = datetime.now(timezone.utc) + timedelta(
            seconds=self._wait_seconds
        )

        log.warning(
            "Rate limit hit — pausing execution for %d min (resume ≈ %s). "
            "Error: %s",
            self._wait_seconds // 60,
            self._resume_at.strftime("%H:%M UTC"),
            (error_msg or "unknown")[:200],
        )

        await _call(self._on_pause, self._wait_seconds, self._resume_at)

        # Background sleep — other callers block on the gate
        self._wait_task = asyncio.create_task(self._wait_and_resume())

        # Wait for the pause to finish before returning so the caller
        # can immediately retry
        await self._gate.wait()

    async def _wait_and_resume(self) -> None:
        """Background task: sleep then open the gate."""
        try:
            await asyncio.sleep(self._wait_seconds)
        except asyncio.CancelledError:
            # Execution was cancelled while paused — open gate so
            # waiters can unblock and see the cancellation.
            self._paused = False
            self._resume_at = None
            self._gate.set()
            return

        self._paused = False
        self._resume_at = None
        self._gate.set()

        log.info("Rate-limit pause ended — resuming execution")
        await _call(self._on_resume)

    # -- Cleanup ------------------------------------------------------------

    def cancel(self) -> None:
        """Cancel any pending wait (call when execution is cancelled)."""
        if self._wait_task and not self._wait_task.done():
            self._wait_task.cancel()


# ---------------------------------------------------------------------------
# Runner wrapper
# ---------------------------------------------------------------------------


class RateLimitAwareRunner:
    """Wraps an :class:`AgentRunner` with automatic rate-limit pause + retry.

    When a task returns a rate-limited result:

    1. Signals the shared :class:`RateLimitPauser` to enter paused state.
    2. The pauser sleeps for the configured window (e.g. 5 h).
    3. All spawns — this one and others — block on the gate.
    4. When the gate reopens the task is retried.

    Parameters
    ----------
    inner:
        The real runner (``PiRunner`` or ``ClaudeCodeRunner``).
    pauser:
        Shared pauser instance for this execution.
    max_retries:
        How many times to retry a single task after rate-limit pauses
        before giving up and returning the failure.
    """

    def __init__(
        self,
        inner: AgentRunner,
        pauser: RateLimitPauser,
        max_retries: int = 3,
    ):
        self._inner = inner
        self._pauser = pauser
        self._max_retries = max_retries

    async def spawn(self, config: RunnerConfig) -> RunnerResult:
        for attempt in range(self._max_retries + 1):
            # Block if another task already triggered a pause
            await self._pauser.wait_if_paused()

            result = await self._inner.spawn(config)

            if not is_rate_limit_error(result):
                return result

            if attempt < self._max_retries:
                log.warning(
                    "Task %s hit rate limit (attempt %d/%d) — triggering pause",
                    config.task_id,
                    attempt + 1,
                    self._max_retries + 1,
                )
                # pause() blocks until the wait completes, then we loop
                await self._pauser.pause(result.stderr)
            else:
                log.error(
                    "Task %s still rate-limited after %d pause cycles — giving up",
                    config.task_id,
                    self._max_retries + 1,
                )

        return result  # last (failed) result

    def extract_final_output(self, stdout: str) -> str:
        return self._inner.extract_final_output(stdout)
