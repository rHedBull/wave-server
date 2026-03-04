"""Tests for the stream-json log parser and task log formatter."""

import json

import pytest

from wave_server.engine.log_parser import (
    AssistantTurn,
    ParsedLog,
    ToolCall,
    ToolResult,
    _format_duration,
    _summarize_tool_input,
    format_task_log,
    parse_stream_json,
)


# ── Helpers ─────────────────────────────────────────────────────────


def _make_stream(*events: dict) -> str:
    """Build a stream-json string from event dicts."""
    return "\n".join(json.dumps(e) for e in events)


def _init_event(**kwargs) -> dict:
    return {"type": "system", "subtype": "init", "model": "claude-sonnet-4-20250514", **kwargs}


def _assistant_event(text: str = "", tool_calls: list | None = None) -> dict:
    content = []
    if text:
        content.append({"type": "text", "text": text})
    for tc in (tool_calls or []):
        content.append({
            "type": "tool_use",
            "id": tc.get("id", "toolu_123"),
            "name": tc["name"],
            "input": tc.get("input", {}),
        })
    return {
        "type": "assistant",
        "message": {
            "model": "claude-sonnet-4-20250514",
            "content": content,
        },
    }


def _tool_result_event(tool_use_id: str = "toolu_123", content: str = "", is_error: bool = False, name: str = "") -> dict:
    event = {
        "type": "tool",
        "tool_use_id": tool_use_id,
        "content": content,
        "is_error": is_error,
    }
    if name:
        event["tool"] = {"name": name}
    return event


def _result_event(result: str = "", **kwargs) -> dict:
    return {
        "type": "result",
        "subtype": "success",
        "result": result,
        "duration_ms": kwargs.get("duration_ms", 5000),
        "total_cost_usd": kwargs.get("total_cost_usd", 0.05),
        "num_turns": kwargs.get("num_turns", 3),
        "usage": {
            "input_tokens": kwargs.get("input_tokens", 1000),
            "output_tokens": kwargs.get("output_tokens", 500),
            "cache_read_input_tokens": kwargs.get("cache_read_input_tokens", 0),
            "cache_creation_input_tokens": kwargs.get("cache_creation_input_tokens", 0),
        },
    }


# ── parse_stream_json tests ────────────────────────────────────────


class TestParseStreamJson:
    def test_empty_input(self):
        log = parse_stream_json("")
        assert log.model == ""
        assert log.turns == []
        assert log.final_result == ""

    def test_malformed_lines_skipped(self):
        raw = "not json\n{bad json too\n"
        log = parse_stream_json(raw)
        assert log.turns == []

    def test_init_extracts_model(self):
        raw = _make_stream(_init_event(model="claude-opus-4-6"))
        log = parse_stream_json(raw)
        assert log.model == "claude-opus-4-6"

    def test_assistant_text_turn(self):
        raw = _make_stream(
            _assistant_event(text="I'll read the file now."),
        )
        log = parse_stream_json(raw)
        assert len(log.turns) == 1
        turn = log.turns[0]
        assert isinstance(turn, AssistantTurn)
        assert turn.text == "I'll read the file now."
        assert turn.tool_calls == []

    def test_assistant_tool_call(self):
        raw = _make_stream(
            _assistant_event(
                text="Let me check the file.",
                tool_calls=[{"name": "Read", "input": {"path": "src/main.py"}, "id": "toolu_abc"}],
            ),
        )
        log = parse_stream_json(raw)
        assert len(log.turns) == 1
        turn = log.turns[0]
        assert isinstance(turn, AssistantTurn)
        assert turn.text == "Let me check the file."
        assert len(turn.tool_calls) == 1
        assert turn.tool_calls[0].name == "Read"
        assert "src/main.py" in turn.tool_calls[0].input_summary

    def test_multiple_tool_calls_in_one_turn(self):
        raw = _make_stream(
            _assistant_event(
                tool_calls=[
                    {"name": "Read", "input": {"path": "a.py"}, "id": "t1"},
                    {"name": "Read", "input": {"path": "b.py"}, "id": "t2"},
                ],
            ),
        )
        log = parse_stream_json(raw)
        turn = log.turns[0]
        assert isinstance(turn, AssistantTurn)
        assert len(turn.tool_calls) == 2

    def test_tool_result(self):
        raw = _make_stream(
            _tool_result_event(
                tool_use_id="toolu_abc",
                content="file contents here",
                name="Read",
            ),
        )
        log = parse_stream_json(raw)
        assert len(log.turns) == 1
        tr = log.turns[0]
        assert isinstance(tr, ToolResult)
        assert tr.content == "file contents here"
        assert tr.name == "Read"
        assert not tr.is_error

    def test_tool_result_error(self):
        raw = _make_stream(
            _tool_result_event(
                content="File not found",
                is_error=True,
            ),
        )
        log = parse_stream_json(raw)
        tr = log.turns[0]
        assert isinstance(tr, ToolResult)
        assert tr.is_error

    def test_result_event(self):
        raw = _make_stream(
            _result_event(
                result="All tests pass.",
                duration_ms=12345,
                total_cost_usd=0.0234,
                num_turns=5,
                input_tokens=2000,
                output_tokens=800,
                cache_read_input_tokens=500,
            ),
        )
        log = parse_stream_json(raw)
        assert log.final_result == "All tests pass."
        assert log.duration_ms == 12345
        assert log.total_cost_usd == 0.0234
        assert log.num_turns == 5
        assert log.input_tokens == 2000
        assert log.output_tokens == 800
        assert log.cache_read_tokens == 500

    def test_full_conversation(self):
        """Test parsing a realistic multi-turn conversation."""
        raw = _make_stream(
            _init_event(),
            _assistant_event(
                text="I'll implement the function.",
                tool_calls=[{"name": "Read", "input": {"path": "src/lib.py"}}],
            ),
            _tool_result_event(content="def old_func(): pass"),
            _assistant_event(
                text="Now I'll update it.",
                tool_calls=[{
                    "name": "Edit",
                    "input": {
                        "path": "src/lib.py",
                        "oldText": "def old_func(): pass",
                        "newText": "def new_func():\n    return 42",
                    },
                }],
            ),
            _tool_result_event(content="Successfully edited"),
            _assistant_event(
                text="Let me run the tests.",
                tool_calls=[{"name": "Bash", "input": {"command": "pytest tests/"}}],
            ),
            _tool_result_event(content="3 passed"),
            _assistant_event(text="All tests pass. Implementation complete."),
            _result_event(result="All tests pass. Implementation complete.", num_turns=4),
        )
        log = parse_stream_json(raw)
        assert log.model == "claude-sonnet-4-20250514"
        assert log.num_turns == 4
        # 4 assistant turns + 3 tool results = 7 entries
        assert len(log.turns) == 7
        assert isinstance(log.turns[0], AssistantTurn)
        assert isinstance(log.turns[1], ToolResult)

    def test_model_from_assistant_message(self):
        """Model can come from assistant message if no init event."""
        raw = _make_stream(
            {"type": "assistant", "message": {"model": "claude-haiku-3", "content": [{"type": "text", "text": "hi"}]}},
        )
        log = parse_stream_json(raw)
        assert log.model == "claude-haiku-3"

    def test_tool_result_with_list_content(self):
        """Tool result content can be a list of content blocks."""
        raw = _make_stream({
            "type": "tool",
            "tool_use_id": "t1",
            "tool": {"name": "Read"},
            "content": [
                {"type": "text", "text": "line 1"},
                {"type": "text", "text": "line 2"},
            ],
            "is_error": False,
        })
        log = parse_stream_json(raw)
        tr = log.turns[0]
        assert isinstance(tr, ToolResult)
        assert "line 1" in tr.content
        assert "line 2" in tr.content

    def test_rate_limit_events_ignored(self):
        raw = _make_stream(
            {"type": "rate_limit_event", "rate_limit_info": {"status": "ok"}},
            _assistant_event(text="hello"),
        )
        log = parse_stream_json(raw)
        assert len(log.turns) == 1

    def test_hook_events_ignored(self):
        raw = _make_stream(
            {"type": "system", "subtype": "hook_started", "hook_name": "test"},
            {"type": "system", "subtype": "hook_response", "hook_name": "test"},
            _assistant_event(text="hello"),
        )
        log = parse_stream_json(raw)
        assert len(log.turns) == 1


# ── _summarize_tool_input tests ────────────────────────────────────


class TestSummarizeToolInput:
    def test_bash(self):
        result = _summarize_tool_input("Bash", {"command": "npm test"})
        assert result == "npm test"

    def test_bash_long_command_truncated(self):
        cmd = "x" * 600
        result = _summarize_tool_input("Bash", {"command": cmd})
        assert len(result) <= 503  # 500 + "..."
        assert result.endswith("...")

    def test_read(self):
        result = _summarize_tool_input("Read", {"path": "src/main.py"})
        assert result == "src/main.py"

    def test_read_with_offset_limit(self):
        result = _summarize_tool_input("Read", {"path": "big.py", "offset": 100, "limit": 50})
        assert "big.py" in result
        assert "offset=100" in result
        assert "limit=50" in result

    def test_write(self):
        result = _summarize_tool_input("Write", {"path": "out.py", "content": "hello\nworld"})
        assert "out.py" in result
        assert "hello" in result

    def test_edit(self):
        result = _summarize_tool_input("Edit", {
            "path": "f.py",
            "oldText": "old code",
            "newText": "new code",
        })
        assert "f.py" in result
        assert "old code" in result
        assert "new code" in result

    def test_grep(self):
        result = _summarize_tool_input("Grep", {"pattern": "TODO", "path": "src/"})
        assert "TODO" in result
        assert "src/" in result

    def test_glob(self):
        result = _summarize_tool_input("Glob", {"pattern": "**/*.py"})
        assert "**/*.py" in result

    def test_agent(self):
        result = _summarize_tool_input("Agent", {"agent": "scout", "task": "find auth code"})
        assert "scout" in result
        assert "find auth code" in result

    def test_empty_input(self):
        assert _summarize_tool_input("Bash", {}) == ""

    def test_unknown_tool(self):
        result = _summarize_tool_input("CustomTool", {"key": "value"})
        assert "key" in result
        assert "value" in result


# ── _format_duration tests ─────────────────────────────────────────


class TestFormatDuration:
    def test_milliseconds(self):
        assert _format_duration(500) == "500ms"

    def test_seconds(self):
        assert _format_duration(5000) == "5.0s"

    def test_minutes(self):
        assert _format_duration(125000) == "2m 05s"

    def test_zero(self):
        assert _format_duration(0) == "0ms"


# ── format_task_log tests ──────────────────────────────────────────


class TestFormatTaskLog:
    def _make_log(self, **overrides) -> str:
        defaults = {
            "task_id": "w1-auth-t1",
            "title": "Implement auth service",
            "agent": "worker",
            "phase": "feature:auth",
            "exit_code": 0,
            "duration_ms": 30000,
            "timed_out": False,
            "prompt": "You are implementing code.\n## Your Task\nImplement auth.",
            "parsed": ParsedLog(
                model="claude-sonnet-4-20250514",
                turns=[
                    AssistantTurn(text="I'll implement auth.", tool_calls=[
                        ToolCall(name="Bash", input_summary="pytest tests/"),
                    ]),
                    ToolResult(tool_use_id="t1", name="Bash", content="3 passed"),
                    AssistantTurn(text="All done."),
                ],
                final_result="Auth implemented.",
                duration_ms=30000,
                total_cost_usd=0.05,
                input_tokens=2000,
                output_tokens=500,
                num_turns=2,
            ),
            "extracted_output": "Auth implemented.",
        }
        defaults.update(overrides)
        return format_task_log(**defaults)

    def test_header_present(self):
        log = self._make_log()
        assert "# ✅ 🔨 w1-auth-t1: Implement auth service" in log

    def test_metadata_fields(self):
        log = self._make_log()
        assert "**Agent**: worker" in log
        assert "**Phase**: feature:auth" in log
        assert "**Status**: passed" in log
        assert "**Duration**: 30.0s" in log
        assert "**Model**: claude-sonnet-4-20250514" in log
        assert "**Cost**: $0.0500" in log
        assert "**Tokens**:" in log
        assert "2,000 in" in log
        assert "500 out" in log

    def test_prompt_section(self):
        log = self._make_log()
        assert "## Prompt" in log
        assert "You are implementing code." in log

    def test_execution_trace(self):
        log = self._make_log()
        assert "## Execution Trace" in log
        assert "### Turn 1" in log
        assert "I'll implement auth." in log
        assert "→ Bash" in log
        assert "pytest tests/" in log

    def test_tool_result_in_trace(self):
        log = self._make_log()
        assert "← result (Bash)" in log
        assert "3 passed" in log

    def test_final_output(self):
        log = self._make_log()
        assert "## Final Output" in log
        assert "Auth implemented." in log

    def test_failed_task(self):
        log = self._make_log(exit_code=1)
        assert "# ❌ 🔨 w1-auth-t1" in log
        assert "failed (exit 1)" in log

    def test_timed_out_task(self):
        log = self._make_log(timed_out=True)
        assert "# ⏰ 🔨 w1-auth-t1" in log
        assert "TIMED OUT" in log

    def test_test_writer_agent(self):
        log = self._make_log(agent="test-writer")
        assert "🧪" in log

    def test_verifier_agent(self):
        log = self._make_log(agent="wave-verifier")
        assert "🔍" in log

    def test_long_prompt_truncated(self):
        log = self._make_log(prompt="x" * 5000)
        assert "chars truncated" in log

    def test_no_output(self):
        log = self._make_log(extracted_output="")
        assert "*(no output)*" in log

    def test_error_tool_result(self):
        parsed = ParsedLog(turns=[
            ToolResult(tool_use_id="t1", name="Bash", content="command failed", is_error=True),
        ])
        log = self._make_log(parsed=parsed)
        assert "❌ ERROR" in log
