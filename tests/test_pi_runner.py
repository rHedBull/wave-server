"""Tests for PiRunner — extract_final_output, parse_pi_json, spawn(), and integration."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from wave_server.engine.runner import AgentRunner, PiRunner, get_runner
from wave_server.engine.log_parser import (
    AssistantTurn,
    ParsedLog,
    ToolResult,
    format_task_log,
    parse_pi_json,
)
from wave_server.engine.types import RunnerConfig, RunnerResult


# ── Sample pi JSONL output (captured from real pi run) ─────────

SAMPLE_PI_OUTPUT = '\n'.join([
    '{"type":"session","version":3,"id":"test-session","timestamp":"2026-03-07T17:08:16.131Z","cwd":"/tmp/test"}',
    '{"type":"agent_start"}',
    '{"type":"turn_start"}',
    '{"type":"message_end","message":{"role":"user","content":[{"type":"text","text":"Create hello.txt"}]}}',
    '{"type":"message_end","message":{"role":"assistant","content":[{"type":"toolCall","id":"tool_1","name":"write","arguments":{"path":"hello.txt","content":"hello world"}}],"model":"claude-sonnet-4-5","usage":{"input":1353,"output":79,"cacheRead":0,"cacheWrite":0,"totalTokens":1432,"cost":{"input":0.004059,"output":0.000395,"cacheRead":0,"cacheWrite":0,"total":0.004454}}}}',
    '{"type":"message_end","message":{"role":"toolResult","content":[{"type":"text","text":"Successfully wrote 11 bytes to hello.txt"}]}}',
    '{"type":"turn_end"}',
    '{"type":"turn_start"}',
    '{"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"Created hello.txt with content \'hello world\'."}],"model":"claude-sonnet-4-5","usage":{"input":1461,"output":21,"cacheRead":0,"cacheWrite":0,"totalTokens":1482,"cost":{"input":0.004383,"output":0.000105,"cacheRead":0,"cacheWrite":0,"total":0.004488}}}}',
    '{"type":"turn_end"}',
    '{"type":"agent_end","messages":[{"role":"user","content":[{"type":"text","text":"Create hello.txt"}]},{"role":"assistant","content":[{"type":"toolCall","id":"tool_1","name":"write","arguments":{"path":"hello.txt","content":"hello world"}}],"usage":{"input":1353,"output":79,"cost":{"total":0.004454}}},{"role":"toolResult","content":[{"type":"text","text":"Successfully wrote 11 bytes to hello.txt"}]},{"role":"assistant","content":[{"type":"text","text":"Created hello.txt with content \'hello world\'."}],"usage":{"input":1461,"output":21,"cost":{"total":0.004488}}}]}',
])


# ── PiRunner.extract_final_output ──────────────────────────────


class TestPiExtractFinalOutput:
    def setup_method(self):
        self.runner = PiRunner()

    def test_extracts_last_assistant_text(self):
        result = self.runner.extract_final_output(SAMPLE_PI_OUTPUT)
        assert result == "Created hello.txt with content 'hello world'."

    def test_agent_end_extracts_from_messages(self):
        """agent_end event with messages array should yield final assistant text."""
        stdout = json.dumps({
            "type": "agent_end",
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "Do something"}]},
                {"role": "assistant", "content": [{"type": "text", "text": "Done!"}]},
            ],
        })
        result = self.runner.extract_final_output(stdout)
        assert result == "Done!"

    def test_message_end_assistant_text(self):
        stdout = json.dumps({
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Here is the answer."}],
            },
        })
        result = self.runner.extract_final_output(stdout)
        assert result == "Here is the answer."

    def test_skips_tool_call_only_messages(self):
        """Messages with only toolCall content (no text) should not yield output."""
        stdout = json.dumps({
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [{"type": "toolCall", "name": "bash", "arguments": {"command": "ls"}}],
            },
        })
        result = self.runner.extract_final_output(stdout)
        # No text content, should fall back
        assert "(no output)" not in result or result == "(no output)"

    def test_skips_user_messages(self):
        stdout = json.dumps({
            "type": "message_end",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "User prompt"}],
            },
        })
        result = self.runner.extract_final_output(stdout)
        # User messages are not extracted
        assert result != "User prompt"

    def test_empty_stdout(self):
        assert self.runner.extract_final_output("") == "(no output)"

    def test_whitespace_only(self):
        assert self.runner.extract_final_output("  \n  \n  ") == "(no output)"

    def test_malformed_json_skipped(self):
        lines = [
            "not json",
            json.dumps({
                "type": "message_end",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "Valid"}]},
            }),
        ]
        stdout = "\n".join(lines)
        assert self.runner.extract_final_output(stdout) == "Valid"

    def test_fallback_to_last_lines(self):
        lines = [f"Log line {i}" for i in range(20)]
        stdout = "\n".join(lines)
        result = self.runner.extract_final_output(stdout)
        assert "Log line 19" in result
        assert "Log line 10" in result
        assert "Log line 9" not in result

    def test_last_assistant_text_wins(self):
        """When multiple assistant messages, the last text is returned."""
        lines = [
            json.dumps({
                "type": "message_end",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "First"}]},
            }),
            json.dumps({
                "type": "message_end",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "Second"}]},
            }),
        ]
        stdout = "\n".join(lines)
        result = self.runner.extract_final_output(stdout)
        assert result == "Second"


# ── parse_pi_json ──────────────────────────────────────────────


class TestParsePiJson:
    def test_model_extracted(self):
        parsed = parse_pi_json(SAMPLE_PI_OUTPUT)
        assert parsed.model == "claude-sonnet-4-5"

    def test_turns_parsed(self):
        parsed = parse_pi_json(SAMPLE_PI_OUTPUT)
        # Should have: assistant turn (tool call), tool result, assistant turn (text)
        assert len(parsed.turns) >= 2

    def test_tool_call_extracted(self):
        parsed = parse_pi_json(SAMPLE_PI_OUTPUT)
        from wave_server.engine.log_parser import AssistantTurn
        tool_turns = [t for t in parsed.turns if isinstance(t, AssistantTurn) and t.tool_calls]
        assert len(tool_turns) >= 1
        tc = tool_turns[0].tool_calls[0]
        assert tc.name == "write"
        assert "hello.txt" in tc.input_summary

    def test_tool_result_extracted(self):
        parsed = parse_pi_json(SAMPLE_PI_OUTPUT)
        from wave_server.engine.log_parser import ToolResult
        tool_results = [t for t in parsed.turns if isinstance(t, ToolResult)]
        assert len(tool_results) >= 1
        assert "Successfully wrote" in tool_results[0].content

    def test_cost_summed(self):
        parsed = parse_pi_json(SAMPLE_PI_OUTPUT)
        # 0.004454 + 0.004488 = 0.008942
        assert abs(parsed.total_cost_usd - 0.008942) < 0.0001

    def test_input_tokens(self):
        parsed = parse_pi_json(SAMPLE_PI_OUTPUT)
        # 1353 + 1461 = 2814
        assert parsed.input_tokens == 2814

    def test_output_tokens(self):
        parsed = parse_pi_json(SAMPLE_PI_OUTPUT)
        # 79 + 21 = 100
        assert parsed.output_tokens == 100

    def test_final_result(self):
        parsed = parse_pi_json(SAMPLE_PI_OUTPUT)
        assert parsed.final_result == "Created hello.txt with content 'hello world'."

    def test_num_turns(self):
        parsed = parse_pi_json(SAMPLE_PI_OUTPUT)
        assert parsed.num_turns == 2

    def test_empty_input(self):
        parsed = parse_pi_json("")
        assert parsed.model == ""
        assert parsed.turns == []
        assert parsed.total_cost_usd == 0.0

    def test_malformed_lines_skipped(self):
        raw = "not json\n" + SAMPLE_PI_OUTPUT
        parsed = parse_pi_json(raw)
        assert parsed.model == "claude-sonnet-4-5"

    def test_cache_tokens(self):
        """Cache read/write tokens are extracted from usage."""
        line = json.dumps({
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "cached"}],
                "model": "test-model",
                "usage": {
                    "input": 100,
                    "output": 50,
                    "cacheRead": 500,
                    "cacheWrite": 200,
                    "cost": {"total": 0.001},
                },
            },
        })
        parsed = parse_pi_json(line)
        assert parsed.cache_read_tokens == 500
        assert parsed.cache_creation_tokens == 200


# ── get_runner ─────────────────────────────────────────────────


class TestGetRunnerPi:
    def test_returns_pi_runner(self):
        runner = get_runner("pi")
        assert isinstance(runner, PiRunner)

    def test_pi_runner_is_agent_runner(self):
        runner = PiRunner()
        assert isinstance(runner, AgentRunner)

    def test_unknown_runtime_mentions_pi(self):
        with pytest.raises(ValueError, match="pi"):
            get_runner("nonexistent")


# ── PiRunner.spawn() — subprocess tests ───────────────────────


class TestPiRunnerSpawn:
    """Tests for PiRunner.spawn() with mocked subprocess."""

    @pytest.mark.asyncio
    async def test_command_construction_without_model(self):
        """Verify the exact CLI flags passed to pi."""
        config = RunnerConfig(
            task_id="t1", prompt="Do something", cwd="/tmp"
        )
        captured_cmd = []

        async def fake_exec(*args, **kwargs):
            captured_cmd.extend(args)
            proc = AsyncMock()
            proc.communicate = AsyncMock(return_value=(b"output", b""))
            proc.returncode = 0
            proc.kill = MagicMock()
            return proc

        with patch("wave_server.engine.runner.asyncio.create_subprocess_exec", side_effect=fake_exec):
            runner = PiRunner()
            await runner.spawn(config)

        assert captured_cmd[0] == "pi"
        assert "--print" in captured_cmd
        assert "--mode" in captured_cmd
        idx = captured_cmd.index("--mode")
        assert captured_cmd[idx + 1] == "json"
        assert "--no-extensions" in captured_cmd
        assert "--no-skills" in captured_cmd
        assert "--no-prompt-templates" in captured_cmd
        assert "--no-themes" in captured_cmd
        assert "--no-session" in captured_cmd
        assert "--tools" in captured_cmd
        idx = captured_cmd.index("--tools")
        assert captured_cmd[idx + 1] == "read,bash,edit,write"
        # Prompt is the last argument
        assert captured_cmd[-1] == "Do something"
        # No --model flag
        assert "--model" not in captured_cmd

    @pytest.mark.asyncio
    async def test_command_construction_with_model(self):
        """--model flag should be included when config.model is set."""
        config = RunnerConfig(
            task_id="t1", prompt="Do it", cwd="/tmp", model="claude-opus-4"
        )
        captured_cmd = []

        async def fake_exec(*args, **kwargs):
            captured_cmd.extend(args)
            proc = AsyncMock()
            proc.communicate = AsyncMock(return_value=(b"output", b""))
            proc.returncode = 0
            proc.kill = MagicMock()
            return proc

        with patch("wave_server.engine.runner.asyncio.create_subprocess_exec", side_effect=fake_exec):
            runner = PiRunner()
            await runner.spawn(config)

        assert "--model" in captured_cmd
        idx = captured_cmd.index("--model")
        assert captured_cmd[idx + 1] == "claude-opus-4"
        # Prompt still last
        assert captured_cmd[-1] == "Do it"

    @pytest.mark.asyncio
    async def test_env_merging(self):
        """config.env should be merged with os.environ."""
        config = RunnerConfig(
            task_id="t1", prompt="x", cwd="/tmp",
            env={"MY_VAR": "hello", "OTHER": "world"},
        )
        captured_kwargs = {}

        async def fake_exec(*args, **kwargs):
            captured_kwargs.update(kwargs)
            proc = AsyncMock()
            proc.communicate = AsyncMock(return_value=(b"", b""))
            proc.returncode = 0
            proc.kill = MagicMock()
            return proc

        with patch("wave_server.engine.runner.asyncio.create_subprocess_exec", side_effect=fake_exec):
            runner = PiRunner()
            await runner.spawn(config)

        env = captured_kwargs["env"]
        assert env["MY_VAR"] == "hello"
        assert env["OTHER"] == "world"
        # Should also contain inherited env vars (e.g. PATH)
        assert "PATH" in env

    @pytest.mark.asyncio
    async def test_no_env_passes_none(self):
        """When config.env is None, spawn_env should be None (inherit)."""
        config = RunnerConfig(task_id="t1", prompt="x", cwd="/tmp")
        captured_kwargs = {}

        async def fake_exec(*args, **kwargs):
            captured_kwargs.update(kwargs)
            proc = AsyncMock()
            proc.communicate = AsyncMock(return_value=(b"", b""))
            proc.returncode = 0
            proc.kill = MagicMock()
            return proc

        with patch("wave_server.engine.runner.asyncio.create_subprocess_exec", side_effect=fake_exec):
            runner = PiRunner()
            await runner.spawn(config)

        assert captured_kwargs["env"] is None

    @pytest.mark.asyncio
    async def test_cwd_passed_through(self):
        """config.cwd is forwarded to subprocess."""
        config = RunnerConfig(task_id="t1", prompt="x", cwd="/my/project")
        captured_kwargs = {}

        async def fake_exec(*args, **kwargs):
            captured_kwargs.update(kwargs)
            proc = AsyncMock()
            proc.communicate = AsyncMock(return_value=(b"", b""))
            proc.returncode = 0
            proc.kill = MagicMock()
            return proc

        with patch("wave_server.engine.runner.asyncio.create_subprocess_exec", side_effect=fake_exec):
            runner = PiRunner()
            await runner.spawn(config)

        assert captured_kwargs["cwd"] == "/my/project"

    @pytest.mark.asyncio
    async def test_timeout_kills_process(self):
        """When timeout expires, process should be killed and timed_out=True."""
        config = RunnerConfig(
            task_id="t1", prompt="x", cwd="/tmp", timeout_ms=100,
        )

        async def fake_exec(*args, **kwargs):
            proc = AsyncMock()
            # First communicate call raises timeout, second returns after kill
            call_count = 0

            async def communicate_side_effect():
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    raise asyncio.TimeoutError()
                return (b"partial", b"err")

            proc.communicate = communicate_side_effect
            proc.kill = MagicMock()
            proc.returncode = -9
            return proc

        with patch("wave_server.engine.runner.asyncio.create_subprocess_exec", side_effect=fake_exec):
            runner = PiRunner()
            result = await runner.spawn(config)

        assert result.timed_out is True
        assert result.stdout == "partial"
        assert result.stderr == "err"

    @pytest.mark.asyncio
    async def test_file_not_found_graceful(self):
        """When pi CLI is not installed, return a helpful error."""
        config = RunnerConfig(task_id="t1", prompt="x", cwd="/tmp")

        with patch(
            "wave_server.engine.runner.asyncio.create_subprocess_exec",
            side_effect=FileNotFoundError("No such file"),
        ):
            runner = PiRunner()
            result = await runner.spawn(config)

        assert result.exit_code == 1
        assert result.timed_out is False
        assert "pi CLI not found" in result.stderr

    @pytest.mark.asyncio
    async def test_nonzero_exit_code(self):
        """Non-zero exit code is captured correctly."""
        config = RunnerConfig(task_id="t1", prompt="x", cwd="/tmp")

        async def fake_exec(*args, **kwargs):
            proc = AsyncMock()
            proc.communicate = AsyncMock(return_value=(b"out", b"error details"))
            proc.returncode = 2
            proc.kill = MagicMock()
            return proc

        with patch("wave_server.engine.runner.asyncio.create_subprocess_exec", side_effect=fake_exec):
            runner = PiRunner()
            result = await runner.spawn(config)

        assert result.exit_code == 2
        assert result.stdout == "out"
        assert result.stderr == "error details"
        assert result.timed_out is False

    @pytest.mark.asyncio
    async def test_no_timeout_when_timeout_ms_none(self):
        """When timeout_ms is None, asyncio.wait_for should get timeout=None."""
        config = RunnerConfig(task_id="t1", prompt="x", cwd="/tmp", timeout_ms=None)

        wait_for_timeout = []
        original_wait_for = asyncio.wait_for

        async def tracking_wait_for(coro, timeout=None):
            wait_for_timeout.append(timeout)
            return await original_wait_for(coro, timeout=timeout)

        async def fake_exec(*args, **kwargs):
            proc = AsyncMock()
            proc.communicate = AsyncMock(return_value=(b"", b""))
            proc.returncode = 0
            proc.kill = MagicMock()
            return proc

        with (
            patch("wave_server.engine.runner.asyncio.create_subprocess_exec", side_effect=fake_exec),
            patch("wave_server.engine.runner.asyncio.wait_for", side_effect=tracking_wait_for),
        ):
            runner = PiRunner()
            await runner.spawn(config)

        assert wait_for_timeout == [None]

    @pytest.mark.asyncio
    async def test_timeout_ms_converted_to_seconds(self):
        """timeout_ms=5000 should become timeout=5.0 seconds."""
        config = RunnerConfig(task_id="t1", prompt="x", cwd="/tmp", timeout_ms=5000)

        wait_for_timeout = []
        original_wait_for = asyncio.wait_for

        async def tracking_wait_for(coro, timeout=None):
            wait_for_timeout.append(timeout)
            return await original_wait_for(coro, timeout=timeout)

        async def fake_exec(*args, **kwargs):
            proc = AsyncMock()
            proc.communicate = AsyncMock(return_value=(b"", b""))
            proc.returncode = 0
            proc.kill = MagicMock()
            return proc

        with (
            patch("wave_server.engine.runner.asyncio.create_subprocess_exec", side_effect=fake_exec),
            patch("wave_server.engine.runner.asyncio.wait_for", side_effect=tracking_wait_for),
        ):
            runner = PiRunner()
            await runner.spawn(config)

        assert wait_for_timeout == [5.0]


# ── parse_pi_json — edge cases ────────────────────────────────


class TestParsePiJsonEdgeCases:
    """Edge cases and complex scenarios for parse_pi_json."""

    def test_mixed_text_and_tool_call_in_one_message(self):
        """Assistant message with both text and toolCall content blocks."""
        line = json.dumps({
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "I'll create that file now."},
                    {"type": "toolCall", "id": "tc1", "name": "write",
                     "arguments": {"path": "out.txt", "content": "data"}},
                ],
                "model": "test",
                "usage": {"input": 100, "output": 50, "cacheRead": 0, "cacheWrite": 0,
                          "cost": {"total": 0.001}},
            },
        })
        parsed = parse_pi_json(line)
        assert len(parsed.turns) == 1
        turn = parsed.turns[0]
        assert isinstance(turn, AssistantTurn)
        assert "I'll create that file now." in turn.text
        assert len(turn.tool_calls) == 1
        assert turn.tool_calls[0].name == "write"

    def test_multiple_tool_calls_in_one_message(self):
        """Assistant message with multiple toolCall blocks."""
        line = json.dumps({
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "toolCall", "id": "tc1", "name": "read",
                     "arguments": {"path": "a.py"}},
                    {"type": "toolCall", "id": "tc2", "name": "bash",
                     "arguments": {"command": "ls"}},
                    {"type": "toolCall", "id": "tc3", "name": "edit",
                     "arguments": {"path": "b.py", "oldText": "x", "newText": "y"}},
                ],
                "model": "test",
                "usage": {"input": 200, "output": 80, "cacheRead": 0, "cacheWrite": 0,
                          "cost": {"total": 0.002}},
            },
        })
        parsed = parse_pi_json(line)
        assert len(parsed.turns) == 1
        turn = parsed.turns[0]
        assert isinstance(turn, AssistantTurn)
        assert len(turn.tool_calls) == 3
        assert turn.tool_calls[0].name == "read"
        assert turn.tool_calls[1].name == "bash"
        assert turn.tool_calls[2].name == "edit"

    def test_agent_end_with_no_assistant_messages(self):
        """agent_end where no assistant messages exist — final_result stays empty."""
        line = json.dumps({
            "type": "agent_end",
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "hello"}]},
            ],
        })
        parsed = parse_pi_json(line)
        assert parsed.final_result == ""
        assert parsed.num_turns == 0

    def test_message_end_missing_usage(self):
        """Assistant message with no usage field — should not crash."""
        line = json.dumps({
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "No usage here"}],
                "model": "test",
            },
        })
        parsed = parse_pi_json(line)
        assert len(parsed.turns) == 1
        assert parsed.total_cost_usd == 0.0
        assert parsed.input_tokens == 0
        assert parsed.output_tokens == 0

    def test_message_end_missing_cost_in_usage(self):
        """Usage present but cost field missing — cost stays 0."""
        line = json.dumps({
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "partial usage"}],
                "model": "test",
                "usage": {"input": 100, "output": 50},
            },
        })
        parsed = parse_pi_json(line)
        assert parsed.input_tokens == 100
        assert parsed.output_tokens == 50
        assert parsed.total_cost_usd == 0.0

    def test_tool_result_with_multiple_content_blocks(self):
        """toolResult with multiple text content blocks."""
        line = json.dumps({
            "type": "message_end",
            "message": {
                "role": "toolResult",
                "content": [
                    {"type": "text", "text": "line 1"},
                    {"type": "text", "text": "line 2"},
                    {"type": "text", "text": "line 3"},
                ],
            },
        })
        parsed = parse_pi_json(line)
        results = [t for t in parsed.turns if isinstance(t, ToolResult)]
        assert len(results) == 1
        assert results[0].content == "line 1\nline 2\nline 3"

    def test_unknown_event_types_ignored(self):
        """Events like session, agent_start, turn_start, turn_end are silently skipped."""
        lines = [
            json.dumps({"type": "session", "version": 3}),
            json.dumps({"type": "agent_start"}),
            json.dumps({"type": "turn_start"}),
            json.dumps({"type": "message_update", "delta": "partial"}),
            json.dumps({"type": "tool_execution_start"}),
            json.dumps({"type": "tool_execution_end"}),
            json.dumps({"type": "turn_end"}),
        ]
        parsed = parse_pi_json("\n".join(lines))
        assert parsed.turns == []
        assert parsed.model == ""

    def test_model_from_first_assistant_only(self):
        """Model is taken from the first assistant message that has it."""
        lines = [
            json.dumps({
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "a"}],
                    "model": "first-model",
                    "usage": {"input": 10, "output": 5, "cost": {"total": 0.0}},
                },
            }),
            json.dumps({
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "b"}],
                    "model": "second-model",
                    "usage": {"input": 10, "output": 5, "cost": {"total": 0.0}},
                },
            }),
        ]
        parsed = parse_pi_json("\n".join(lines))
        assert parsed.model == "first-model"

    def test_assistant_with_empty_text_block(self):
        """Empty text blocks should not create a turn."""
        line = json.dumps({
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": ""}],
                "model": "test",
            },
        })
        parsed = parse_pi_json(line)
        assert parsed.turns == []

    def test_cost_accumulates_across_many_turns(self):
        """Cost from all assistant messages sums correctly."""
        lines = []
        for i in range(5):
            lines.append(json.dumps({
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": f"turn {i}"}],
                    "model": "test",
                    "usage": {
                        "input": 100, "output": 20,
                        "cacheRead": 10, "cacheWrite": 5,
                        "cost": {"total": 0.001},
                    },
                },
            }))
        parsed = parse_pi_json("\n".join(lines))
        assert parsed.input_tokens == 500
        assert parsed.output_tokens == 100
        assert parsed.cache_read_tokens == 50
        assert parsed.cache_creation_tokens == 25
        assert abs(parsed.total_cost_usd - 0.005) < 0.0001


# ── parse_pi_json + format_task_log integration ───────────────


class TestParsePiJsonFormatIntegration:
    """Verify parse_pi_json output feeds correctly into format_task_log."""

    def test_pi_parsed_log_produces_valid_markdown(self):
        parsed = parse_pi_json(SAMPLE_PI_OUTPUT)
        log = format_task_log(
            task_id="1-1",
            title="Create hello.txt",
            agent="worker",
            phase="foundation",
            exit_code=0,
            duration_ms=5000,
            timed_out=False,
            prompt="Create hello.txt with content 'hello world'.",
            parsed=parsed,
            extracted_output="Created hello.txt with content 'hello world'.",
        )
        assert "# ✅ 🔨 1-1: Create hello.txt" in log
        assert "**Agent**: worker" in log
        assert "**Model**: claude-sonnet-4-5" in log
        assert "**Cost**: $0.0089" in log
        assert "2,814 in" in log  # input tokens
        assert "100 out" in log  # output tokens
        assert "## Execution Trace" in log
        assert "**→ write**" in log  # tool call
        assert "hello.txt" in log
        assert "## Final Output" in log
        assert "Created hello.txt" in log

    def test_pi_failed_task_format(self):
        parsed = parse_pi_json(SAMPLE_PI_OUTPUT)
        log = format_task_log(
            task_id="1-2",
            title="Broken task",
            agent="test-writer",
            phase="foundation",
            exit_code=1,
            duration_ms=3000,
            timed_out=False,
            prompt="Do something that fails",
            parsed=parsed,
            extracted_output="Error occurred",
        )
        assert "❌" in log
        assert "🧪" in log  # test-writer emoji
        assert "failed (exit 1)" in log

    def test_pi_timed_out_task_format(self):
        parsed = parse_pi_json("")  # Empty — timed out before output
        log = format_task_log(
            task_id="1-3",
            title="Slow task",
            agent="worker",
            phase="foundation",
            exit_code=0,
            duration_ms=60000,
            timed_out=True,
            prompt="Task that times out",
            parsed=parsed,
            extracted_output="",
        )
        assert "⏰" in log
        assert "TIMED OUT" in log
        assert "*(no output)*" in log
