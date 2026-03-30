"""Tests for enforcement config generation, prompt sections, and verifier output parsing."""

from wave_server.engine.enforcement import (
    generate_enforcement_config,
    enforcement_to_prompt_section,
    is_verifier_failure,
)
from wave_server.engine.types import FileAccessRules


# ── generate_enforcement_config ────────────────────────────────


class TestGenerateEnforcementConfig:
    def test_empty_rules_returns_empty_dict(self):
        rules = FileAccessRules()
        assert generate_enforcement_config(rules) == {}

    def test_read_only_returns_only_read_only(self):
        rules = FileAccessRules(
            read_only=True,
            allow_write=["a.py"],
            allow_read=["b.py"],
            protected_paths=["c.py"],
            safe_bash_only=True,
        )
        config = generate_enforcement_config(rules)
        assert config == {"readOnly": True}
        # read_only short-circuits — no other keys
        assert "allowWrite" not in config
        assert "safeBashOnly" not in config

    def test_allow_write(self):
        rules = FileAccessRules(allow_write=["src/main.py", "src/utils.py"])
        config = generate_enforcement_config(rules)
        assert config["allowWrite"] == ["src/main.py", "src/utils.py"]

    def test_allow_read(self):
        rules = FileAccessRules(allow_read=["docs/spec.md"])
        config = generate_enforcement_config(rules)
        assert config["allowRead"] == ["docs/spec.md"]

    def test_protected_paths(self):
        rules = FileAccessRules(protected_paths=[".env", "secrets.json"])
        config = generate_enforcement_config(rules)
        assert config["protectedPaths"] == [".env", "secrets.json"]

    def test_safe_bash_only(self):
        rules = FileAccessRules(safe_bash_only=True)
        config = generate_enforcement_config(rules)
        assert config["safeBashOnly"] is True

    def test_all_fields_combined(self):
        rules = FileAccessRules(
            allow_write=["a.py"],
            allow_read=["b.py"],
            protected_paths=["c.py"],
            safe_bash_only=True,
        )
        config = generate_enforcement_config(rules)
        assert config["allowWrite"] == ["a.py"]
        assert config["allowRead"] == ["b.py"]
        assert config["protectedPaths"] == ["c.py"]
        assert config["safeBashOnly"] is True

    def test_empty_lists_not_included(self):
        rules = FileAccessRules(allow_write=[], allow_read=[], protected_paths=[])
        config = generate_enforcement_config(rules)
        assert config == {}

    def test_none_fields_not_included(self):
        rules = FileAccessRules(allow_write=None, allow_read=None)
        config = generate_enforcement_config(rules)
        assert "allowWrite" not in config
        assert "allowRead" not in config


# ── enforcement_to_prompt_section ─────────────────────────────


class TestEnforcementToPromptSection:
    def test_empty_rules(self):
        rules = FileAccessRules()
        section = enforcement_to_prompt_section(rules)
        assert "## File Access Rules" in section
        # No specific rules
        assert "ONLY" not in section
        assert "NEVER" not in section

    def test_read_only_short_circuits(self):
        rules = FileAccessRules(
            read_only=True,
            allow_write=["a.py"],
            protected_paths=["b.py"],
            safe_bash_only=True,
        )
        section = enforcement_to_prompt_section(rules)
        assert "READ-ONLY" in section
        assert "Do NOT modify" in section
        # Should NOT include allow_write or other rules
        assert "ONLY write" not in section
        assert "NEVER modify" not in section

    def test_allow_write(self):
        rules = FileAccessRules(allow_write=["src/main.py", "src/utils.py"])
        section = enforcement_to_prompt_section(rules)
        assert "ONLY write/edit" in section
        assert "src/main.py" in section
        assert "src/utils.py" in section

    def test_protected_paths(self):
        rules = FileAccessRules(protected_paths=[".env", "secrets.json"])
        section = enforcement_to_prompt_section(rules)
        assert "NEVER modify" in section
        assert ".env" in section
        assert "secrets.json" in section

    def test_safe_bash_only(self):
        rules = FileAccessRules(safe_bash_only=True)
        section = enforcement_to_prompt_section(rules)
        assert "safe bash commands" in section
        assert "no rm" in section

    def test_combined_rules(self):
        rules = FileAccessRules(
            allow_write=["a.py"],
            protected_paths=["b.py"],
            safe_bash_only=True,
        )
        section = enforcement_to_prompt_section(rules)
        assert "ONLY write/edit" in section
        assert "a.py" in section
        assert "NEVER modify" in section
        assert "b.py" in section
        assert "safe bash" in section

    def test_returns_string(self):
        rules = FileAccessRules()
        assert isinstance(enforcement_to_prompt_section(rules), str)


# ── is_verifier_failure ───────────────────────────────────────


class TestIsVerifierFailure:
    def test_empty_output(self):
        assert is_verifier_failure("") is False

    def test_pass_status(self):
        output = '{"status": "pass", "summary": "All good", "readyForNextWave": true}'
        assert is_verifier_failure(output) is False

    def test_fail_status(self):
        output = (
            '{"status": "fail", "summary": "Missing files", "readyForNextWave": false}'
        )
        assert is_verifier_failure(output) is True

    def test_fail_status_with_code_fences(self):
        output = '```json\n{"status": "fail", "summary": "Tests failed"}\n```'
        assert is_verifier_failure(output) is True

    def test_pass_status_with_code_fences(self):
        output = '```json\n{"status": "pass", "summary": "All clear"}\n```'
        assert is_verifier_failure(output) is False

    def test_ready_for_next_wave_false(self):
        output = '{"status": "fail", "readyForNextWave": false}'
        assert is_verifier_failure(output) is True

    def test_ready_for_next_wave_true_with_pass(self):
        output = '{"status": "pass", "readyForNextWave": true}'
        assert is_verifier_failure(output) is False

    def test_text_before_json(self):
        output = (
            "The three required files are confirmed missing:\n"
            "1. `schema.py` — **MISSING**\n\n"
            '```json\n{"status": "fail", "summary": "Files missing", "readyForNextWave": false}\n```'
        )
        assert is_verifier_failure(output) is True

    def test_text_before_json_pass(self):
        output = (
            "All files verified successfully.\n\n"
            '```json\n{"status": "pass", "summary": "Looks good", "readyForNextWave": true}\n```'
        )
        assert is_verifier_failure(output) is False

    def test_no_json_with_fail_pattern(self):
        output = 'The verification result was "status": "fail" for this task.'
        assert is_verifier_failure(output) is True

    def test_no_json_no_pattern(self):
        output = "Some random verifier output with no structured result."
        assert is_verifier_failure(output) is False

    def test_nested_json_with_issues(self):
        output = """{
  "status": "fail",
  "summary": "Projects feature files are missing",
  "failedStep": "file_existence",
  "missingFiles": ["backbone/app/schemas/project.py"],
  "tasks": [{"id": "w2-projects-t3", "status": "fail"}],
  "issues": [{"severity": "error", "description": "Schema missing"}],
  "readyForNextWave": false
}"""
        assert is_verifier_failure(output) is True

    def test_malformed_json_with_fail_pattern(self):
        output = '{"status": "fail", bad json here'
        assert is_verifier_failure(output) is True

    def test_none_like_output(self):
        assert is_verifier_failure("(no output)") is False

    def test_case_insensitive_fallback(self):
        output = '"Status": "FAIL"'
        assert is_verifier_failure(output) is True
