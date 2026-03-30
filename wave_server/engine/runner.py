"""Agent subprocess runner — spawns claude/pi subprocesses.

Implements AgentRunner protocol with ClaudeCodeRunner as the default.
Uses asyncio.create_subprocess_exec (not shell) to avoid injection.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from wave_server.engine.types import RunnerConfig, RunnerResult


@runtime_checkable
class AgentRunner(Protocol):
    async def spawn(self, config: RunnerConfig) -> RunnerResult: ...
    def extract_final_output(self, stdout: str) -> str: ...


@dataclass
class _PiOutputFailure:
    """Result of inspecting pi's JSON output for fatal errors."""
    error: str
    rate_limited: bool = False


# Patterns that indicate a rate-limit / overload (vs. a real task failure)
_RATE_LIMIT_PATTERNS = [
    "rate_limit",
    "rate limit",
    "429",
    "overloaded",
    "overloaded_error",
    "529",
    "too many requests",
]


def _is_rate_limit_message(msg: str) -> bool:
    """Check if an error message indicates a rate limit / overload."""
    lower = msg.lower()
    return any(p in lower for p in _RATE_LIMIT_PATTERNS)


def _detect_pi_output_failure(stdout: str) -> _PiOutputFailure | None:
    """Scan pi's JSON output for fatal errors that pi doesn't reflect in its exit code.

    Pi CLI exits 0 even when it encounters rate limits, API overload errors,
    or other fatal conditions. This function inspects the structured output
    to detect these failures.

    Returns a _PiOutputFailure if a failure is detected, None otherwise.
    """
    has_auto_retry_failure = False
    auto_retry_error = ""
    last_agent_end_error = ""
    last_stop_reason = ""
    # Tracks whether *any* retry attempt produced real work (text or tool calls).
    # Intentionally not reset between retries: if any attempt did useful work,
    # the non-retry fallback path won't flag it as a total failure.
    # This is safe because auto_retry_end (which always wins) handles the
    # retry-exhausted case regardless of this flag.
    had_any_successful_output = False

    for line in stdout.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue

        msg_type = msg.get("type", "")

        # Check auto_retry_end — pi retried and all attempts failed
        if msg_type == "auto_retry_end" and not msg.get("success", True):
            has_auto_retry_failure = True
            auto_retry_error = msg.get("finalError", "")

        # Check agent_end for error stop reason
        if msg_type == "agent_end":
            messages = msg.get("messages", [])
            if messages:
                last_msg = messages[-1]
                error_msg = last_msg.get("errorMessage", "")
                stop_reason = last_msg.get("stopReason", "")
                if error_msg:
                    last_agent_end_error = error_msg
                if stop_reason:
                    last_stop_reason = stop_reason
                # Check if any assistant message produced real content
                for m in messages:
                    if m.get("role") == "assistant":
                        for block in m.get("content", []):
                            if block.get("type") == "text" and block.get("text", "").strip():
                                had_any_successful_output = True
                            elif block.get("type") == "toolCall":
                                had_any_successful_output = True

        # Check message_end/turn_end for error stop reasons
        if msg_type in ("message_end", "turn_end"):
            inner = msg.get("message", {})
            if inner.get("stopReason") == "error":
                error_msg = inner.get("errorMessage", "")
                if error_msg:
                    last_agent_end_error = error_msg
                    last_stop_reason = "error"

    # auto_retry_end with success=false is the strongest signal
    if has_auto_retry_failure:
        error = f"Pi task failed after retries exhausted: {auto_retry_error}"
        return _PiOutputFailure(
            error=error,
            rate_limited=_is_rate_limit_message(auto_retry_error),
        )

    # agent_end with error stop reason and no useful output
    if last_stop_reason == "error" and not had_any_successful_output:
        error = f"Pi task ended with error (no output produced): {last_agent_end_error}"
        return _PiOutputFailure(
            error=error,
            rate_limited=_is_rate_limit_message(last_agent_end_error),
        )

    return None


class PiRunner:
    """Spawns `pi` subprocesses with JSON mode output.

    Uses minimal tool set (read, write, edit, bash) and no extensions/hooks
    for ~23x reduction in system prompt overhead vs Claude Code CLI.
    """

    async def spawn(self, config: RunnerConfig) -> RunnerResult:
        pi_bin = shutil.which("pi")
        if not pi_bin:
            return RunnerResult(
                exit_code=1,
                stdout="",
                stderr="pi CLI not found. Install pi: npm install -g @mariozechner/pi-coding-agent",
                timed_out=False,
            )
        cmd = [
            pi_bin,
            "--print",
            "--mode", "json",
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-themes",
            "--no-session",
            "--tools", "read,bash,edit,write",
        ]
        if config.model:
            cmd += ["--model", config.model]
        cmd.append(config.prompt)

        try:
            # Ensure nvm/node PATH is available in subprocess
            spawn_env = {**os.environ}
            if config.env:
                spawn_env.update(config.env)
            # Add pi's bin dir to PATH if not already present
            pi_dir = os.path.dirname(pi_bin)
            if pi_dir not in spawn_env.get("PATH", ""):
                spawn_env["PATH"] = pi_dir + ":" + spawn_env.get("PATH", "")

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=config.cwd,
                env=spawn_env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            timed_out = False
            timeout_s = config.timeout_ms / 1000 if config.timeout_ms else None

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout_s
                )
            except asyncio.TimeoutError:
                proc.kill()
                stdout_bytes, stderr_bytes = await proc.communicate()
                timed_out = True

            stdout_str = stdout_bytes.decode("utf-8", errors="replace")
            stderr_str = stderr_bytes.decode("utf-8", errors="replace")
            exit_code = proc.returncode or 0

            # Pi exits 0 even on fatal errors (rate limits, overloaded, etc.).
            # Inspect the JSON output to detect these failures.
            rate_limited = False
            if exit_code == 0 and not timed_out:
                detected = _detect_pi_output_failure(stdout_str)
                if detected:
                    exit_code = 1
                    stderr_str = (stderr_str + "\n" + detected.error).strip()
                    rate_limited = detected.rate_limited

            return RunnerResult(
                exit_code=exit_code,
                stdout=stdout_str,
                stderr=stderr_str,
                timed_out=timed_out,
                rate_limited=rate_limited,
            )
        except FileNotFoundError as e:
            return RunnerResult(
                exit_code=1,
                stdout="",
                stderr=f"pi spawn failed (FileNotFoundError): {e}",
                timed_out=False,
            )

    def extract_final_output(self, stdout: str) -> str:
        """Extract the final result text from pi's JSON mode output."""
        result_parts: list[str] = []
        for line in stdout.split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
                if msg.get("type") == "agent_end":
                    # Get text from the last assistant message
                    messages = msg.get("messages", [])
                    for m in reversed(messages):
                        if m.get("role") == "assistant":
                            for block in m.get("content", []):
                                if block.get("type") == "text" and block.get("text", "").strip():
                                    result_parts.append(block["text"])
                            if result_parts:
                                break
                elif msg.get("type") == "message_end":
                    m = msg.get("message", {})
                    if m.get("role") == "assistant":
                        for block in m.get("content", []):
                            if block.get("type") == "text" and block.get("text", "").strip():
                                result_parts.append(block["text"])
            except (json.JSONDecodeError, KeyError):
                continue

        if result_parts:
            return result_parts[-1]  # Last assistant text is the final output

        # Fallback: return last non-empty lines
        lines = [l for l in stdout.split("\n") if l.strip()]
        return "\n".join(lines[-10:]) if lines else "(no output)"


class ClaudeCodeRunner:
    """Spawns `claude` subprocesses with stream-json output.

    Uses asyncio.create_subprocess_exec (no shell) for safety.
    """

    async def spawn(self, config: RunnerConfig) -> RunnerResult:
        # create_subprocess_exec passes args directly, no shell interpretation
        cmd = [
            "claude",
            "--print",
            "--verbose",
            "--output-format",
            "stream-json",
            "--dangerously-skip-permissions",
        ]
        if config.model:
            cmd += ["--model", config.model]
        cmd.append(config.prompt)

        try:
            # Merge project env vars into the subprocess environment
            spawn_env = None
            if config.env:
                spawn_env = {**os.environ, **config.env}

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=config.cwd,
                env=spawn_env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            timed_out = False
            timeout_s = config.timeout_ms / 1000 if config.timeout_ms else None

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout_s
                )
            except asyncio.TimeoutError:
                proc.kill()
                stdout_bytes, stderr_bytes = await proc.communicate()
                timed_out = True

            return RunnerResult(
                exit_code=proc.returncode or 0,
                stdout=stdout_bytes.decode("utf-8", errors="replace"),
                stderr=stderr_bytes.decode("utf-8", errors="replace"),
                timed_out=timed_out,
            )
        except FileNotFoundError:
            return RunnerResult(
                exit_code=1,
                stdout="",
                stderr="claude CLI not found. Install Claude Code: https://docs.anthropic.com/en/docs/claude-code",
                timed_out=False,
            )

    def extract_final_output(self, stdout: str) -> str:
        """Extract the final result text from claude's stream-json output."""
        result_parts: list[str] = []
        for line in stdout.split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
                if msg.get("type") == "result":
                    result_text = msg.get("result", "")
                    if result_text:
                        result_parts.append(result_text)
                elif msg.get("type") == "assistant" and "content" in msg:
                    for block in msg["content"]:
                        if block.get("type") == "text":
                            result_parts.append(block["text"])
            except (json.JSONDecodeError, KeyError):
                continue

        if result_parts:
            return "\n".join(result_parts)

        # Fallback: return last non-empty lines
        lines = [l for l in stdout.split("\n") if l.strip()]
        return "\n".join(lines[-10:]) if lines else "(no output)"


def get_runner(runtime: str = "pi") -> AgentRunner:
    """Get the appropriate runner for the given runtime."""
    if runtime == "claude":
        return ClaudeCodeRunner()
    if runtime == "pi":
        return PiRunner()
    raise ValueError(f"Unknown runtime: {runtime}. Available: claude, pi")
