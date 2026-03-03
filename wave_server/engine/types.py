from __future__ import annotations

from dataclasses import dataclass, field


# ── Plan Structure ─────────────────────────────────────────────────


@dataclass
class Task:
    id: str
    title: str
    agent: str = "worker"  # "test-writer" | "worker" | "wave-verifier"
    files: list[str] = field(default_factory=list)
    depends: list[str] = field(default_factory=list)
    spec_refs: list[str] = field(default_factory=list)
    test_files: list[str] = field(default_factory=list)
    description: str = ""


@dataclass
class Feature:
    name: str
    files: list[str] = field(default_factory=list)
    tasks: list[Task] = field(default_factory=list)


@dataclass
class Wave:
    name: str
    description: str = ""
    foundation: list[Task] = field(default_factory=list)
    features: list[Feature] = field(default_factory=list)
    integration: list[Task] = field(default_factory=list)


@dataclass
class Plan:
    goal: str = ""
    data_schemas: str = ""
    waves: list[Wave] = field(default_factory=list)


# ── Execution Results ──────────────────────────────────────────────


@dataclass
class TaskResult:
    id: str
    title: str
    agent: str
    exit_code: int
    output: str
    stderr: str
    duration_ms: int
    stdout: str = ""
    timed_out: bool = False


@dataclass
class FeatureResult:
    name: str
    branch: str
    task_results: list[TaskResult] = field(default_factory=list)
    passed: bool = False
    error: str | None = None


@dataclass
class WaveResult:
    wave: str
    foundation_results: list[TaskResult] = field(default_factory=list)
    feature_results: list[FeatureResult] = field(default_factory=list)
    integration_results: list[TaskResult] = field(default_factory=list)
    passed: bool = False


@dataclass
class MergeResult:
    source: str
    target: str
    success: bool
    had_changes: bool
    error: str | None = None


# ── DAG ────────────────────────────────────────────────────────────


@dataclass
class DAGLevel:
    tasks: list[Task]
    parallel: bool  # true if >1 task at this level


# ── Runner ─────────────────────────────────────────────────────────


@dataclass
class RunnerConfig:
    task_id: str
    prompt: str
    cwd: str
    timeout_ms: int | None = None


@dataclass
class RunnerResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


# ── Execution State (for resume) ───────────────────────────────────


@dataclass
class ExecutionState:
    plan_file: str
    started_at: str
    updated_at: str
    current_wave: int = 0
    task_states: dict[str, str] = field(default_factory=dict)  # "done" | "failed" | "skipped"


# ── Git Worktree ───────────────────────────────────────────────────


@dataclass
class FeatureWorktree:
    feature_name: str
    branch: str
    dir: str
    repo_root: str


@dataclass
class SubWorktree:
    task_id: str
    branch: str
    dir: str
    parent_branch: str


# ── File Access Enforcement ────────────────────────────────────────


@dataclass
class FileAccessRules:
    allow_write: list[str] | None = None
    allow_read: list[str] | None = None
    protected_paths: list[str] | None = None
    read_only: bool = False
    safe_bash_only: bool = False


# ── Progress ───────────────────────────────────────────────────────


@dataclass
class ProgressUpdate:
    phase: str  # "foundation" | "features" | "merge" | "integration"
    features: list[dict[str, str]] | None = None
    current_tasks: list[dict[str, str]] | None = None
