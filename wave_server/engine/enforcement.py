"""File access enforcement extension generator.

Generates Claude Code enforcement rules from FileAccessRules to restrict
which files an agent can read/write during task execution.

Also provides ``is_verifier_failure`` — a utility that inspects wave-verifier
output to detect semantic verification failures (the agent exits 0 but its
JSON report says ``"status": "fail"``).
"""

from __future__ import annotations

import json
import re

from wave_server.engine.types import FileAccessRules


def generate_enforcement_config(rules: FileAccessRules) -> dict:
    """Generate a Claude Code enforcement config from FileAccessRules."""
    config: dict = {}

    if rules.read_only:
        config["readOnly"] = True
        return config

    if rules.allow_write:
        config["allowWrite"] = rules.allow_write

    if rules.allow_read:
        config["allowRead"] = rules.allow_read

    if rules.protected_paths:
        config["protectedPaths"] = rules.protected_paths

    if rules.safe_bash_only:
        config["safeBashOnly"] = True

    return config


def enforcement_to_prompt_section(rules: FileAccessRules) -> str:
    """Convert FileAccessRules to a prompt section for the agent."""
    lines: list[str] = ["\n## File Access Rules"]

    if rules.read_only:
        lines.append("- You are in READ-ONLY mode. Do NOT modify any files.")
        return "\n".join(lines)

    if rules.allow_write:
        lines.append(
            f"- You may ONLY write/edit these files: {', '.join(rules.allow_write)}"
        )

    if rules.protected_paths:
        lines.append(
            f"- NEVER modify these protected files: {', '.join(rules.protected_paths)}"
        )

    if rules.safe_bash_only:
        lines.append(
            "- Only use safe bash commands (no rm, mv, or other destructive operations)"
        )

    return "\n".join(lines)


# ── Wave-verifier output inspection ───────────────────────────


_CODE_FENCE_RE = re.compile(r"```\w*\s*\n?")


def is_verifier_failure(output: str) -> bool:
    """Return ``True`` if *output* from a wave-verifier task indicates failure.

    The wave-verifier agent is instructed to emit JSON with a ``"status"``
    field (``"pass"`` or ``"fail"``).  However the agent process itself
    always exits 0 because it completed its work — even when the
    verification found problems.  This function parses the output to
    detect the semantic failure so the engine can treat it correctly.
    """
    if not output:
        return False

    # Try JSON parsing — output may be wrapped in markdown code fences
    cleaned = output.strip()
    cleaned = _CODE_FENCE_RE.sub("", cleaned).rstrip("`").strip()

    # The output may contain explanatory text before/after the JSON.
    # Try to extract the first JSON object.
    json_start = cleaned.find("{")
    json_end = cleaned.rfind("}")
    if json_start != -1 and json_end > json_start:
        candidate = cleaned[json_start : json_end + 1]
        try:
            data = json.loads(candidate)
            if isinstance(data, dict):
                status = data.get("status", "").lower()
                if status == "fail":
                    return True
                ready = data.get("readyForNextWave")
                if ready is False:
                    return True
                return False
        except (json.JSONDecodeError, ValueError):
            pass

    # Fallback: look for the pattern in raw text
    if re.search(r'"status"\s*:\s*"fail"', output, re.IGNORECASE):
        return True

    return False
