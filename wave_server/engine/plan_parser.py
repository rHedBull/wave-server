"""Plan Markdown parser — feature-based format v2.

Plans must declare their format version via an HTML comment:
  <!-- format: v2 -->

Supported versions:
  v2 — feature-based format (current, required)

Expected v2 structure:
  <!-- format: v2 -->
  ## Project Structure   (optional — injected into agent prompts)
  ## Environment         (optional — injected into agent prompts)
  ## Data Schemas        (optional — injected into agent prompts)
  ## Wave N: <name>
  ### Foundation
  ### Feature: <name>
  ### Integration
  #### Task <id>: <title>
"""

from __future__ import annotations

import re

from wave_server.engine.types import Feature, Plan, Task, Wave


SUPPORTED_VERSIONS = {"v2"}
CURRENT_VERSION = "v2"


def _extract_format_version(markdown: str) -> str | None:
    """Extract format version from <!-- format: vN --> comment."""
    m = re.search(r"<!--\s*format:\s*(v\d+)\s*-->", markdown)
    return m.group(1) if m else None


def parse_plan(markdown: str) -> Plan:
    """Parse a plan markdown document into a Plan dataclass.

    Requires a <!-- format: v2 --> version marker. Raises ValueError if
    the version is missing, unsupported, or the plan uses legacy format.
    """
    version = _extract_format_version(markdown)

    if version is None:
        raise ValueError(
            "Plan is missing a format version. "
            f"Add '<!-- format: {CURRENT_VERSION} -->' near the top of the plan."
        )

    if version not in SUPPORTED_VERSIONS:
        raise ValueError(
            f"Unsupported plan format '{version}'. "
            f"Supported versions: {', '.join(sorted(SUPPORTED_VERSIONS))}."
        )

    return _parse_v2(markdown)


def extract_plan_section(markdown: str, section_name: str) -> str:
    """Extract a named ## section from plan markdown.

    Returns the full content between `## <section_name>` and the next `## ` heading
    (or `---` separator). Returns empty string if not found.
    """
    lines = markdown.split("\n")
    capturing = False
    captured: list[str] = []
    header_pattern = re.compile(rf"^## {re.escape(section_name)}", re.IGNORECASE)

    for line in lines:
        if header_pattern.match(line.strip()):
            capturing = True
            captured.append(line)
            continue

        if capturing:
            if re.match(r"^## (?!#)", line) and not header_pattern.match(line):
                break
            if line.strip() == "---":
                break
            captured.append(line)

    return "\n".join(captured).strip()


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
    plan = Plan(
        data_schemas=extract_data_schemas(markdown),
        project_structure=extract_plan_section(markdown, "Project Structure"),
        environment=extract_plan_section(markdown, "Environment"),
    )

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
            i, description_lines = _parse_task_metadata(lines, i, current_task)
            continue

        i += 1

    flush_wave()

    if not plan.goal:
        m = re.search(r"^# Implementation Plan\s*\n+(.+)", markdown, re.MULTILINE)
        if m:
            plan.goal = m.group(1).strip()

    return plan
