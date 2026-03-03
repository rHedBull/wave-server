"""Plan Markdown parser — supports both feature-based format and legacy flat format.

New format:
  ## Wave N: <name>
  ### Foundation
  ### Feature: <name>
  ### Integration
  #### Task <id>: <title>

Legacy format (backward compat):
  ## Wave N: <name>
  ### Task <id>: <title>
  -> wraps all tasks in a single "default" feature
"""

from __future__ import annotations

import re

from wave_server.engine.types import Feature, Plan, Task, Wave


def parse_plan(markdown: str) -> Plan:
    """Parse a plan markdown document into a Plan dataclass.

    Auto-detects new feature-based format vs legacy flat format.
    """
    lines = markdown.split("\n")

    has_feature_headers = any(
        re.match(r"^### (Feature:|Foundation|Integration)", line.strip(), re.IGNORECASE)
        for line in lines
    )

    if has_feature_headers:
        return _parse_v2(markdown)
    return _parse_legacy(markdown)


def extract_data_schemas(markdown: str) -> str:
    """Extract the ## Data Schemas section from plan markdown."""
    lines = markdown.split("\n")
    capturing = False
    captured: list[str] = []

    for line in lines:
        if re.match(r"^## Data Schemas", line.strip(), re.IGNORECASE):
            capturing = True
            captured.append(line)
            continue

        if capturing:
            if re.match(r"^## (?!#)", line) and not re.match(
                r"^## Data Schemas", line, re.IGNORECASE
            ):
                break
            if line.strip() == "---":
                break
            captured.append(line)

    return "\n".join(captured).strip()


def _parse_task_metadata(
    lines: list[str], start: int, task: Task
) -> tuple[int, list[str]]:
    """Parse task metadata lines starting from `start`. Returns (next_index, description_lines)."""
    i = start
    in_description = False
    description_lines: list[str] = []

    while i < len(lines):
        line = lines[i]

        # Agent
        m = re.match(r"^\s*-\s*\*\*Agent\*\*:\s*(.+)", line)
        if m:
            task.agent = m.group(1).strip().replace("`", "")
            in_description = False
            i += 1
            continue

        # Files
        m = re.match(r"^\s*-\s*\*\*Files?\*\*:\s*(.+)", line)
        if m:
            task.files = [
                f.strip().replace("`", "") for f in m.group(1).split(",") if f.strip()
            ]
            in_description = False
            i += 1
            continue

        # Depends
        m = re.match(r"^\s*-\s*\*\*Depends?\*\*:\s*(.+)", line)
        if m:
            raw = m.group(1).strip()
            if raw in ("(none)", "-") or raw.lower() == "none":
                task.depends = []
            else:
                task.depends = [d.strip() for d in raw.split(",") if d.strip()]
            in_description = False
            i += 1
            continue

        # Tests
        m = re.match(r"^\s*-\s*\*\*Tests?\*\*:\s*(.+)", line)
        if m:
            task.test_files = [
                f.strip().replace("`", "") for f in m.group(1).split(",") if f.strip()
            ]
            in_description = False
            i += 1
            continue

        # Spec refs
        m = re.match(r"^\s*-\s*\*\*Spec refs?\*\*:\s*(.+)", line)
        if m:
            task.spec_refs = [r.strip() for r in m.group(1).split(",") if r.strip()]
            in_description = False
            i += 1
            continue

        # Description start
        m = re.match(r"^\s*-\s*\*\*Description\*\*:\s*(.*)", line)
        if m:
            in_description = True
            if m.group(1).strip():
                description_lines.append(m.group(1).strip())
            i += 1
            continue

        # Description continuation
        if in_description:
            if re.match(r"^#{2,4}\s", line) or re.match(
                r"^\s*-\s*\*\*(Agent|Files?|Depends?|Tests?|Spec refs?)\*\*", line
            ):
                in_description = False
                break  # Don't consume this line
            description_lines.append(line)
            i += 1
            continue

        # Not a metadata line and not in description — stop
        if not in_description:
            break

    return i, description_lines


def _parse_v2(markdown: str) -> Plan:
    lines = markdown.split("\n")
    plan = Plan(data_schemas=extract_data_schemas(markdown))

    current_wave: Wave | None = None
    current_section: str | None = None  # "foundation" | "feature" | "integration"
    current_feature: Feature | None = None
    current_task: Task | None = None
    description_lines: list[str] = []
    goal_next_line = False

    def flush_task():
        nonlocal current_task, description_lines
        if current_task:
            current_task.description = "\n".join(description_lines).strip()
            if current_section == "foundation" and current_wave:
                current_wave.foundation.append(current_task)
            elif current_section == "feature" and current_feature:
                current_feature.tasks.append(current_task)
            elif current_section == "integration" and current_wave:
                current_wave.integration.append(current_task)
        current_task = None
        description_lines = []

    def flush_feature():
        nonlocal current_feature
        flush_task()
        if current_feature and current_wave:
            current_wave.features.append(current_feature)
        current_feature = None

    def flush_wave():
        nonlocal current_wave, current_section
        flush_feature()
        flush_task()
        if current_wave:
            plan.waves.append(current_wave)
        current_wave = None
        current_section = None

    i = 0
    while i < len(lines):
        line = lines[i]

        # Goal header
        if re.match(r"^## Goal", line.strip(), re.IGNORECASE):
            goal_next_line = True
            i += 1
            continue
        if goal_next_line and line.strip():
            plan.goal = line.strip()
            goal_next_line = False
            i += 1
            continue
        if goal_next_line and not line.strip():
            i += 1
            continue

        # Wave header
        m = re.match(r"^## Wave \d+:\s*(.+)", line)
        if m:
            flush_wave()
            current_wave = Wave(name=m.group(1).strip())
            current_section = None
            i += 1
            continue

        # Wave description
        if (
            current_wave
            and current_section is None
            and current_task is None
            and line.strip()
            and not line.startswith("#")
            and not line.startswith("---")
        ):
            if not current_wave.description:
                current_wave.description = line.strip()
            i += 1
            continue

        # Foundation section
        if re.match(r"^### Foundation", line.strip(), re.IGNORECASE):
            flush_feature()
            flush_task()
            current_section = "foundation"
            i += 1
            continue

        # Feature section
        m = re.match(r"^### Feature:\s*(.+)", line, re.IGNORECASE)
        if m:
            flush_feature()
            flush_task()
            current_section = "feature"
            current_feature = Feature(name=m.group(1).strip())
            i += 1
            continue

        # Integration section
        if re.match(r"^### Integration", line.strip(), re.IGNORECASE):
            flush_feature()
            flush_task()
            current_section = "integration"
            i += 1
            continue

        # Feature-level Files line
        if current_section == "feature" and current_feature and not current_task:
            m = re.match(r"^Files?:\s*(.+)", line, re.IGNORECASE)
            if m:
                current_feature.files = [
                    f.strip().replace("`", "")
                    for f in m.group(1).split(",")
                    if f.strip()
                ]
                i += 1
                continue

        # Task header
        m = re.match(r"^#{3,4} Task ([\w-]+):\s*(.+)", line)
        if m:
            flush_task()
            current_task = Task(id=m.group(1), title=m.group(2).strip())
            description_lines = []
            i += 1
            # Parse metadata
            i, description_lines = _parse_task_metadata(
                lines, i, current_task
            )
            continue

        i += 1

    flush_wave()

    if not plan.goal:
        m = re.search(r"^# Implementation Plan\s*\n+(.+)", markdown, re.MULTILINE)
        if m:
            plan.goal = m.group(1).strip()

    return plan


def _parse_legacy(markdown: str) -> Plan:
    lines = markdown.split("\n")
    plan = Plan(data_schemas=extract_data_schemas(markdown))

    current_wave: Wave | None = None
    current_task: Task | None = None
    description_lines: list[str] = []
    goal_next_line = False

    def flush_task():
        nonlocal current_task, description_lines
        if current_task and current_wave:
            current_task.description = "\n".join(description_lines).strip()
            if not current_wave.features:
                current_wave.features.append(Feature(name="default"))
            current_wave.features[0].tasks.append(current_task)
        current_task = None
        description_lines = []

    def flush_wave():
        nonlocal current_wave
        flush_task()
        if current_wave and current_wave.features and any(
            f.tasks for f in current_wave.features
        ):
            plan.waves.append(current_wave)
        current_wave = None

    i = 0
    while i < len(lines):
        line = lines[i]

        # Goal header
        if re.match(r"^## Goal", line.strip(), re.IGNORECASE):
            goal_next_line = True
            i += 1
            continue
        if goal_next_line and line.strip():
            plan.goal = line.strip()
            goal_next_line = False
            i += 1
            continue
        if goal_next_line and not line.strip():
            i += 1
            continue

        # Wave header
        m = re.match(r"^## Wave \d+:\s*(.+)", line)
        if m:
            flush_wave()
            current_wave = Wave(name=m.group(1).strip())
            i += 1
            continue

        # Wave description
        if (
            current_wave
            and not current_wave.features
            and current_task is None
            and line.strip()
            and not line.startswith("#")
            and not line.startswith("---")
        ):
            if not current_wave.description:
                current_wave.description = line.strip()
            i += 1
            continue

        # Task header (### level for legacy)
        m = re.match(r"^### Task ([\w-]+):\s*(.+)", line)
        if m:
            flush_task()
            current_task = Task(id=m.group(1), title=m.group(2).strip())
            description_lines = []
            i += 1
            i, description_lines = _parse_task_metadata(
                lines, i, current_task
            )
            continue

        i += 1

    flush_wave()

    if not plan.goal:
        m = re.search(r"^# Implementation Plan\s*\n+(.+)", markdown, re.MULTILINE)
        if m:
            plan.goal = m.group(1).strip()

    return plan
