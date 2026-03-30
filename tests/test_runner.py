"""Tests for ClaudeCodeRunner.extract_final_output and get_runner."""

import json

import pytest

from wave_server.engine.runner import AgentRunner, ClaudeCodeRunner, get_runner


# ── extract_final_output ───────────────────────────────────────


class TestExtractFinalOutput:
    def setup_method(self):
        self.runner = ClaudeCodeRunner()

    def test_result_type_message(self):
        stdout = json.dumps({"type": "result", "result": "Hello world"})
        assert self.runner.extract_final_output(stdout) == "Hello world"

    def test_assistant_type_with_text_blocks(self):
        stdout = json.dumps(
            {
                "type": "assistant",
                "content": [
                    {"type": "text", "text": "First block"},
                    {"type": "text", "text": "Second block"},
                ],
            }
        )
        result = self.runner.extract_final_output(stdout)
        assert "First block" in result
        assert "Second block" in result

    def test_assistant_type_skips_non_text_blocks(self):
        stdout = json.dumps(
            {
                "type": "assistant",
                "content": [
                    {"type": "tool_use", "name": "bash"},
                    {"type": "text", "text": "Result text"},
                ],
            }
        )
        result = self.runner.extract_final_output(stdout)
        assert result == "Result text"

    def test_mixed_message_types(self):
        lines = [
            json.dumps({"type": "system", "text": "Starting"}),
            json.dumps(
                {
                    "type": "assistant",
                    "content": [{"type": "text", "text": "Working..."}],
                }
            ),
            json.dumps({"type": "result", "result": "Final answer"}),
        ]
        stdout = "\n".join(lines)
        result = self.runner.extract_final_output(stdout)
        assert "Working..." in result
        assert "Final answer" in result

    def test_multiple_results_joined(self):
        lines = [
            json.dumps({"type": "result", "result": "Part 1"}),
            json.dumps({"type": "result", "result": "Part 2"}),
        ]
        stdout = "\n".join(lines)
        result = self.runner.extract_final_output(stdout)
        assert result == "Part 1\nPart 2"

    def test_malformed_json_lines_skipped(self):
        lines = [
            "not json at all",
            "{invalid json}",
            json.dumps({"type": "result", "result": "Valid"}),
            "another bad line",
        ]
        stdout = "\n".join(lines)
        assert self.runner.extract_final_output(stdout) == "Valid"

    def test_empty_stdout_returns_no_output(self):
        assert self.runner.extract_final_output("") == "(no output)"

    def test_whitespace_only_returns_no_output(self):
        assert self.runner.extract_final_output("   \n  \n  ") == "(no output)"

    def test_blank_lines_ignored(self):
        lines = [
            "",
            json.dumps({"type": "result", "result": "Got it"}),
            "",
            "",
        ]
        stdout = "\n".join(lines)
        assert self.runner.extract_final_output(stdout) == "Got it"

    def test_no_result_falls_back_to_last_lines(self):
        lines = [f"Log line {i}" for i in range(20)]
        stdout = "\n".join(lines)
        result = self.runner.extract_final_output(stdout)
        # Should return last 10 lines
        assert "Log line 10" in result
        assert "Log line 19" in result
        assert "Log line 9" not in result

    def test_fallback_with_fewer_than_10_lines(self):
        stdout = "Only one line"
        result = self.runner.extract_final_output(stdout)
        assert result == "Only one line"

    def test_empty_result_text_ignored(self):
        stdout = json.dumps({"type": "result", "result": ""})
        # Empty result text is falsy, so falls back to raw lines
        result = self.runner.extract_final_output(stdout)
        # Should fallback since result is empty string
        assert result != ""

    def test_result_with_no_result_key(self):
        stdout = json.dumps({"type": "result"})
        # msg.get("result", "") returns "" which is falsy, so skipped
        result = self.runner.extract_final_output(stdout)
        assert isinstance(result, str)

    def test_assistant_without_content_key(self):
        stdout = json.dumps({"type": "assistant"})
        # No "content" key, so the elif doesn't trigger
        result = self.runner.extract_final_output(stdout)
        assert isinstance(result, str)


# ── get_runner ─────────────────────────────────────────────────


class TestGetRunner:
    def test_returns_claude_runner(self):
        runner = get_runner("claude")
        assert isinstance(runner, ClaudeCodeRunner)

    def test_default_is_pi(self):
        runner = get_runner()
        from wave_server.engine.runner import PiRunner

        assert isinstance(runner, PiRunner)

    def test_unknown_runtime_raises(self):
        with pytest.raises(ValueError, match="Unknown runtime"):
            get_runner("gpt-4")

    def test_unknown_runtime_error_message(self):
        with pytest.raises(ValueError, match="Available: claude"):
            get_runner("nonexistent")


# ── Protocol compliance ────────────────────────────────────────


class TestProtocol:
    def test_claude_runner_is_agent_runner(self):
        runner = ClaudeCodeRunner()
        assert isinstance(runner, AgentRunner)
