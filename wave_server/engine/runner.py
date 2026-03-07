"""Agent subprocess runner — spawns claude/pi subprocesses.

Implements AgentRunner protocol with ClaudeCodeRunner as the default.
Uses asyncio.create_subprocess_exec (not shell) to avoid injection.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Protocol, runtime_checkable

from wave_server.engine.types import RunnerConfig, RunnerResult


@runtime_checkable
class AgentRunner(Protocol):
    async def spawn(self, config: RunnerConfig) -> RunnerResult: ...
    def extract_final_output(self, stdout: str) -> str: ...


class PiRunner:
    """Spawns `pi` subprocesses with JSON mode output.

    Uses minimal tool set (read, write, edit, bash) and no extensions/hooks
    for ~23x reduction in system prompt overhead vs Claude Code CLI.
    """

    async def spawn(self, config: RunnerConfig) -> RunnerResult:
        cmd = [
            "pi",
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
            spawn_env = None
            if config.env:
                spawn_env = {**os.environ, **config.env}

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=config.cwd,
                env=spawn_env,
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
                stderr="pi CLI not found. Install pi: npm install -g @mariozechner/pi-coding-agent",
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


def get_runner(runtime: str = "claude") -> AgentRunner:
    """Get the appropriate runner for the given runtime."""
    if runtime == "claude":
        return ClaudeCodeRunner()
    if runtime == "pi":
        return PiRunner()
    raise ValueError(f"Unknown runtime: {runtime}. Available: claude, pi")
