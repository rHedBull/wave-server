"""File access enforcement extension generator.

Generates Claude Code enforcement rules from FileAccessRules to restrict
which files an agent can read/write during task execution.
"""

from __future__ import annotations

import json

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
