from pathlib import Path

from wave_server.config import settings


def _storage() -> Path:
    return settings.storage_dir


def _ensure(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


# --- Specs ---


def spec_path(sequence_id: str) -> Path:
    return _storage() / "specs" / sequence_id / "spec.md"


def write_spec(sequence_id: str, content: str) -> Path:
    path = _ensure(spec_path(sequence_id))
    path.write_text(content, encoding="utf-8")
    return path


def read_spec(sequence_id: str) -> str | None:
    path = spec_path(sequence_id)
    return path.read_text(encoding="utf-8") if path.exists() else None


# --- Plans ---


def plan_path(sequence_id: str) -> Path:
    return _storage() / "plans" / sequence_id / "plan.md"


def write_plan(sequence_id: str, content: str) -> Path:
    path = _ensure(plan_path(sequence_id))
    path.write_text(content, encoding="utf-8")
    return path


def read_plan(sequence_id: str) -> str | None:
    path = plan_path(sequence_id)
    return path.read_text(encoding="utf-8") if path.exists() else None


# --- Task Output ---


def output_path(execution_id: str, task_id: str) -> Path:
    return _storage() / "output" / execution_id / f"{task_id}.txt"


def write_output(execution_id: str, task_id: str, content: str) -> Path:
    path = _ensure(output_path(execution_id, task_id))
    path.write_text(content, encoding="utf-8")
    return path


def read_output(execution_id: str, task_id: str) -> str | None:
    path = output_path(execution_id, task_id)
    return path.read_text(encoding="utf-8") if path.exists() else None


def has_output(execution_id: str, task_id: str) -> bool:
    return output_path(execution_id, task_id).exists()


# --- Transcripts ---


def transcript_path(execution_id: str, task_id: str) -> Path:
    return _storage() / "transcripts" / execution_id / f"{task_id}.txt"


def write_transcript(execution_id: str, task_id: str, content: str) -> Path:
    path = _ensure(transcript_path(execution_id, task_id))
    path.write_text(content, encoding="utf-8")
    return path


def read_transcript(execution_id: str, task_id: str) -> str | None:
    path = transcript_path(execution_id, task_id)
    return path.read_text(encoding="utf-8") if path.exists() else None


def has_transcript(execution_id: str, task_id: str) -> bool:
    return transcript_path(execution_id, task_id).exists()


# --- Task Logs (human-readable formatted logs) ---


def _agent_suffix(agent: str) -> str:
    if agent == "test-writer":
        return "-test"
    if agent == "wave-verifier":
        return "-verify"
    if agent == "worker":
        return "-impl"
    return ""


def task_log_path(execution_id: str, task_id: str, agent: str = "") -> Path:
    suffix = _agent_suffix(agent)
    return _storage() / "task-logs" / execution_id / f"{task_id}{suffix}.md"


def write_task_log(
    execution_id: str, task_id: str, content: str, agent: str = ""
) -> Path:
    path = _ensure(task_log_path(execution_id, task_id, agent))
    path.write_text(content, encoding="utf-8")
    return path


def read_task_log(execution_id: str, task_id: str, agent: str = "") -> str | None:
    # Try with agent suffix first
    if agent:
        path = task_log_path(execution_id, task_id, agent)
        if path.exists():
            return path.read_text(encoding="utf-8")

    # Fall back to searching for any matching task log
    log_dir = _storage() / "task-logs" / execution_id
    if log_dir.exists():
        for p in log_dir.iterdir():
            if p.stem.startswith(task_id):
                return p.read_text(encoding="utf-8")
    return None


def has_task_log(execution_id: str, task_id: str) -> bool:
    log_dir = _storage() / "task-logs" / execution_id
    if not log_dir.exists():
        return False
    return any(p.stem.startswith(task_id) for p in log_dir.iterdir())


def list_task_logs(execution_id: str) -> list[dict[str, str]]:
    """List all task log files for an execution.

    Returns list of {"task_id": ..., "filename": ..., "agent": ...}.
    """
    log_dir = _storage() / "task-logs" / execution_id
    if not log_dir.exists():
        return []
    result = []
    for p in sorted(log_dir.iterdir()):
        if not p.is_file():
            continue
        name = p.stem
        # Parse task_id and agent suffix
        if name.endswith("-impl"):
            task_id = name[: -len("-impl")]
            agent = "worker"
        elif name.endswith("-test"):
            task_id = name[: -len("-test")]
            agent = "test-writer"
        elif name.endswith("-verify"):
            task_id = name[: -len("-verify")]
            agent = "wave-verifier"
        else:
            task_id = name
            agent = ""
        result.append({"task_id": task_id, "filename": p.name, "agent": agent})
    return result


def search_task_logs(
    execution_id: str,
    query: str,
    *,
    agent: str = "",
    max_context_chars: int = 200,
) -> list[dict]:
    """Full-text search across task logs for an execution.

    Returns list of matches:
      {"task_id", "agent", "filename", "matches": [{"line_num", "snippet"}]}

    Each snippet shows the matching line with `max_context_chars` of surrounding
    context.  Search is case-insensitive.
    """
    log_dir = _storage() / "task-logs" / execution_id
    if not log_dir.exists():
        return []

    query_lower = query.lower()
    results: list[dict] = []

    for p in sorted(log_dir.iterdir()):
        if not p.is_file() or not p.name.endswith(".md"):
            continue

        # Parse filename
        name = p.stem
        if name.endswith("-impl"):
            tid = name[: -len("-impl")]
            file_agent = "worker"
        elif name.endswith("-test"):
            tid = name[: -len("-test")]
            file_agent = "test-writer"
        elif name.endswith("-verify"):
            tid = name[: -len("-verify")]
            file_agent = "wave-verifier"
        else:
            tid = name
            file_agent = ""

        # Filter by agent if specified
        if agent and file_agent != agent:
            continue

        content = p.read_text(encoding="utf-8")
        lines = content.split("\n")
        matches: list[dict] = []

        for i, line in enumerate(lines, 1):
            if query_lower in line.lower():
                # Build snippet with context
                snippet = line.strip()
                if len(snippet) > max_context_chars:
                    # Center around first match
                    idx = snippet.lower().index(query_lower)
                    start = max(0, idx - max_context_chars // 2)
                    end = min(len(snippet), start + max_context_chars)
                    snippet = (
                        ("…" if start > 0 else "")
                        + snippet[start:end]
                        + ("…" if end < len(snippet) else "")
                    )
                matches.append({"line_num": i, "snippet": snippet})

        if matches:
            results.append(
                {
                    "task_id": tid,
                    "agent": file_agent,
                    "filename": p.name,
                    "matches": matches,
                    "match_count": len(matches),
                }
            )

    return results


# --- Logs ---


def log_path(execution_id: str) -> Path:
    return _storage() / "logs" / execution_id / "log.txt"


def write_log(execution_id: str, content: str) -> Path:
    path = _ensure(log_path(execution_id))
    path.write_text(content, encoding="utf-8")
    return path


def append_log(execution_id: str, line: str) -> Path:
    path = _ensure(log_path(execution_id))
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    return path


def read_log(execution_id: str) -> str | None:
    path = log_path(execution_id)
    return path.read_text(encoding="utf-8") if path.exists() else None
