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
