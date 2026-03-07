"""Parse Claude stream-json output into human-readable task logs.

Claude's `--output-format stream-json --verbose` emits JSONL with these event types:
  - {"type": "system", "subtype": "init", ...}    — session info
  - {"type": "assistant", "message": {"content": [...]}} — assistant turns (text + tool_use)
  - {"type": "tool", "tool": {"name": ...}, ...}  — tool results (currently not in -p mode)
  - {"type": "result", "subtype": "success"|"error", "result": "...", "usage": {...}}
  - {"type": "rate_limit_event", ...}              — rate limit warnings

This module converts that into a readable Markdown log showing what the agent
actually did: what it said, what tools it called, and the final result.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass
class ToolCall:
    """A single tool invocation extracted from an assistant message."""
    name: str
    input_summary: str
    tool_use_id: str = ""


@dataclass
class AssistantTurn:
    """One assistant turn: text + optional tool calls."""
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)


@dataclass
class ToolResult:
    """Result from a tool execution."""
    tool_use_id: str
    name: str = ""
    content: str = ""
    is_error: bool = False


@dataclass
class ParsedLog:
    """Fully parsed execution log."""
    model: str = ""
    turns: list[AssistantTurn | ToolResult] = field(default_factory=list)
    final_result: str = ""
    duration_ms: int = 0
    total_cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    num_turns: int = 0
    stop_reason: str | None = None


def parse_stream_json(raw: str) -> ParsedLog:
    """Parse Claude's stream-json JSONL output into a structured log."""
    log = ParsedLog()

    for line in raw.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        event_type = event.get("type", "")

        if event_type == "system" and event.get("subtype") == "init":
            log.model = event.get("model", "")

        elif event_type == "assistant":
            turn = AssistantTurn()
            message = event.get("message", {})
            if not log.model and message.get("model"):
                log.model = message["model"]

            for block in message.get("content", []):
                if block.get("type") == "text":
                    text = block.get("text", "")
                    if text:
                        turn.text += text
                elif block.get("type") == "tool_use":
                    tc = ToolCall(
                        name=block.get("name", "unknown"),
                        input_summary=_summarize_tool_input(
                            block.get("name", ""), block.get("input", {})
                        ),
                        tool_use_id=block.get("id", ""),
                    )
                    turn.tool_calls.append(tc)

            if turn.text or turn.tool_calls:
                log.turns.append(turn)

        elif event_type == "tool":
            # Tool result events (when using verbose mode with tool execution)
            tool_info = event.get("tool", {})
            content = event.get("content", "")
            if isinstance(content, list):
                # Content blocks
                parts = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        parts.append(block.get("text", ""))
                    elif isinstance(block, str):
                        parts.append(block)
                content = "\n".join(parts)
            elif not isinstance(content, str):
                content = str(content)

            tr = ToolResult(
                tool_use_id=event.get("tool_use_id", ""),
                name=tool_info.get("name", ""),
                content=content,
                is_error=event.get("is_error", False),
            )
            log.turns.append(tr)

        elif event_type == "result":
            log.final_result = event.get("result", "")
            log.duration_ms = event.get("duration_ms", 0)
            log.total_cost_usd = event.get("total_cost_usd", 0.0)
            log.num_turns = event.get("num_turns", 0)
            log.stop_reason = event.get("stop_reason")

            usage = event.get("usage", {})
            log.input_tokens = usage.get("input_tokens", 0)
            log.output_tokens = usage.get("output_tokens", 0)
            log.cache_read_tokens = usage.get("cache_read_input_tokens", 0)
            log.cache_creation_tokens = usage.get("cache_creation_input_tokens", 0)

    return log


def parse_pi_json(raw: str) -> ParsedLog:
    """Parse pi's JSON mode JSONL output into a structured log."""
    log = ParsedLog()
    total_cost = 0.0
    total_input = 0
    total_output = 0
    total_cache_read = 0
    total_cache_write = 0

    for line in raw.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        event_type = event.get("type", "")

        if event_type == "message_end":
            message = event.get("message", {})
            role = message.get("role", "")

            if not log.model and message.get("model"):
                log.model = message["model"]

            usage = message.get("usage")
            if usage:
                total_input += usage.get("input", 0)
                total_output += usage.get("output", 0)
                total_cache_read += usage.get("cacheRead", 0)
                total_cache_write += usage.get("cacheWrite", 0)
                cost = usage.get("cost", {})
                total_cost += cost.get("total", 0.0)

            if role == "assistant":
                turn = AssistantTurn()
                for block in message.get("content", []):
                    block_type = block.get("type", "")
                    if block_type == "text":
                        text = block.get("text", "")
                        if text:
                            turn.text += text
                    elif block_type == "toolCall":
                        tc = ToolCall(
                            name=block.get("name", "unknown"),
                            input_summary=_summarize_tool_input(
                                block.get("name", ""), block.get("arguments", {})
                            ),
                            tool_use_id=block.get("id", ""),
                        )
                        turn.tool_calls.append(tc)
                if turn.text or turn.tool_calls:
                    log.turns.append(turn)

            elif role == "toolResult":
                content_parts = []
                for block in message.get("content", []):
                    if block.get("type") == "text":
                        content_parts.append(block.get("text", ""))
                tr = ToolResult(
                    tool_use_id="",
                    name="",
                    content="\n".join(content_parts),
                    is_error=False,
                )
                log.turns.append(tr)

        elif event_type == "agent_end":
            # Extract final text from last assistant message
            messages = event.get("messages", [])
            for m in reversed(messages):
                if m.get("role") == "assistant":
                    for block in m.get("content", []):
                        if block.get("type") == "text":
                            log.final_result = block.get("text", "")
                            break
                    break
            log.num_turns = sum(1 for m in messages if m.get("role") == "assistant")

    log.total_cost_usd = total_cost
    log.input_tokens = total_input
    log.output_tokens = total_output
    log.cache_read_tokens = total_cache_read
    log.cache_creation_tokens = total_cache_write

    return log


def format_task_log(
    *,
    task_id: str,
    title: str,
    agent: str,
    phase: str,
    exit_code: int,
    duration_ms: int,
    timed_out: bool,
    prompt: str,
    parsed: ParsedLog,
    extracted_output: str,
) -> str:
    """Format a parsed log into a human-readable Markdown task log."""
    lines: list[str] = []

    # ── Header ──────────────────────────────────────────────
    agent_emoji = "🧪" if agent == "test-writer" else "🔍" if agent == "wave-verifier" else "🔨"
    status_emoji = "⏰" if timed_out else "✅" if exit_code == 0 else "❌"

    lines.append(f"# {status_emoji} {agent_emoji} {task_id}: {title}")
    lines.append("")
    lines.append(f"- **Agent**: {agent}")
    lines.append(f"- **Phase**: {phase}")
    lines.append(f"- **Status**: {'TIMED OUT' if timed_out else 'passed' if exit_code == 0 else f'failed (exit {exit_code})'}")
    lines.append(f"- **Duration**: {_format_duration(duration_ms)}")
    if parsed.model:
        lines.append(f"- **Model**: {parsed.model}")
    if parsed.total_cost_usd > 0:
        lines.append(f"- **Cost**: ${parsed.total_cost_usd:.4f}")
    if parsed.input_tokens or parsed.output_tokens:
        token_parts = []
        if parsed.input_tokens:
            token_parts.append(f"{parsed.input_tokens:,} in")
        if parsed.output_tokens:
            token_parts.append(f"{parsed.output_tokens:,} out")
        if parsed.cache_read_tokens:
            token_parts.append(f"{parsed.cache_read_tokens:,} cache-read")
        if parsed.cache_creation_tokens:
            token_parts.append(f"{parsed.cache_creation_tokens:,} cache-write")
        lines.append(f"- **Tokens**: {', '.join(token_parts)}")
    if parsed.num_turns:
        lines.append(f"- **Turns**: {parsed.num_turns}")
    lines.append("")

    # ── Prompt ──────────────────────────────────────────────
    lines.append("---")
    lines.append("")
    lines.append("## Prompt")
    lines.append("")
    lines.append("```")
    # Truncate very long prompts
    if len(prompt) > 3000:
        lines.append(prompt[:3000])
        lines.append(f"\n... ({len(prompt) - 3000} chars truncated)")
    else:
        lines.append(prompt)
    lines.append("```")
    lines.append("")

    # ── Conversation ────────────────────────────────────────
    lines.append("---")
    lines.append("")
    lines.append("## Execution Trace")
    lines.append("")

    turn_num = 0
    for entry in parsed.turns:
        if isinstance(entry, AssistantTurn):
            turn_num += 1
            lines.append(f"### Turn {turn_num}")
            lines.append("")

            if entry.text:
                # Truncate very long text blocks
                text = entry.text
                if len(text) > 5000:
                    text = text[:5000] + f"\n\n... ({len(entry.text) - 5000} chars truncated)"
                lines.append(text)
                lines.append("")

            for tc in entry.tool_calls:
                lines.append(f"**→ {tc.name}**")
                if tc.input_summary:
                    lines.append(f"```")
                    lines.append(tc.input_summary)
                    lines.append(f"```")
                lines.append("")

        elif isinstance(entry, ToolResult):
            error_tag = " ❌ ERROR" if entry.is_error else ""
            name_tag = f" ({entry.name})" if entry.name else ""
            lines.append(f"**← result{name_tag}{error_tag}**")
            if entry.content:
                content = entry.content
                if len(content) > 3000:
                    content = content[:3000] + f"\n... ({len(entry.content) - 3000} chars truncated)"
                lines.append("```")
                lines.append(content)
                lines.append("```")
            lines.append("")

    # ── Final Output ────────────────────────────────────────
    lines.append("---")
    lines.append("")
    lines.append("## Final Output")
    lines.append("")
    if extracted_output:
        lines.append(extracted_output)
    else:
        lines.append("*(no output)*")
    lines.append("")

    return "\n".join(lines)


def _summarize_tool_input(tool_name: str, input_data: dict) -> str:
    """Create a concise summary of tool input for the log."""
    if not input_data:
        return ""

    if tool_name in ("Bash", "bash"):
        cmd = input_data.get("command", "")
        if len(cmd) > 500:
            return cmd[:500] + "..."
        return cmd

    if tool_name in ("Read", "read"):
        path = input_data.get("path", "")
        parts = [path]
        if "offset" in input_data:
            parts.append(f"offset={input_data['offset']}")
        if "limit" in input_data:
            parts.append(f"limit={input_data['limit']}")
        return ", ".join(parts)

    if tool_name in ("Write", "write"):
        path = input_data.get("path", "")
        content = input_data.get("content", "")
        length = len(content)
        preview = content[:200].replace("\n", "\\n") if content else ""
        if length > 200:
            preview += f"... ({length} chars)"
        return f"{path}\n{preview}"

    if tool_name in ("Edit", "edit"):
        path = input_data.get("path", "")
        old = input_data.get("oldText", "")[:100].replace("\n", "\\n")
        new = input_data.get("newText", "")[:100].replace("\n", "\\n")
        return f"{path}\n- {old}\n+ {new}"

    if tool_name in ("Grep", "grep"):
        pattern = input_data.get("pattern", "")
        path = input_data.get("path", ".")
        return f"{pattern} in {path}"

    if tool_name in ("Glob", "glob"):
        return input_data.get("pattern", "")

    if tool_name in ("Agent", "subagent"):
        agent = input_data.get("agent", "")
        task = input_data.get("task", "")[:200]
        return f"agent={agent}: {task}"

    # Generic: show as compact JSON
    try:
        s = json.dumps(input_data, indent=None)
        if len(s) > 500:
            return s[:500] + "..."
        return s
    except (TypeError, ValueError):
        return str(input_data)[:500]


def _format_duration(ms: int) -> str:
    """Format milliseconds as human-readable duration."""
    if ms < 1000:
        return f"{ms}ms"
    seconds = ms / 1000
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{minutes}m {secs:02d}s"
