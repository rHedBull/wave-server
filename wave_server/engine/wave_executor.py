"""Wave executor — runs a complete wave: foundation -> features -> merge -> integration.

Ported from TypeScript wave-executor.ts. Simplified for server context:
- Events are emitted via callbacks (server inserts into DB)
- Git worktree operations delegated to git_worktree module
- Runner is pluggable via AgentRunner protocol
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from wave_server.engine.dag import execute_dag, map_concurrent
from wave_server.engine.feature_executor import execute_feature as _execute_feature
from wave_server.engine.git_worktree import (
    cleanup_all,
    commit_task_output,
    create_feature_worktree,
    is_git_repo,
    merge_feature_branches,
)
from wave_server.engine.runner import AgentRunner
from wave_server.engine.types import (
    Feature,
    FeatureResult,
    FeatureWorktree,
    MergeResult,
    ProgressUpdate,
    Task,
    TaskResult,
    Wave,
    WaveResult,
)


@dataclass
class WaveExecutorOptions:
    wave: Wave
    wave_num: int
    runner: AgentRunner
    spec_content: str = ""
    data_schemas: str = ""
    project_structure: str = ""
    environment: str = ""
    project_context: str = ""
    cwd: str = "."
    env: dict[str, str] | None = None
    max_concurrency: int = 4
    skip_task_ids: set[str] = field(default_factory=set)
    repo_root: str | None = None    # separate from cwd for worktree creation
    use_worktrees: bool = True       # can disable for non-git or testing
    model: str | None = None                      # default model for all tasks
    agent_models: dict[str, str] | None = None    # per-agent-type overrides

    # Callbacks (async or sync — async callbacks are awaited)
    on_progress: Callable[[ProgressUpdate], Any] | None = None
    on_task_start: Callable[[str, Task], Any] | None = None
    on_task_end: Callable[[str, Task, TaskResult], Any] | None = None
    on_merge_result: Callable[[MergeResult], Any] | None = None
    on_log: Callable[[str], Any] | None = None


async def _call(fn: Callable | None, *args: Any) -> None:
    """Call a callback, awaiting it if it's async."""
    if fn is None:
        return
    result = fn(*args)
    if inspect.isawaitable(result):
        await result


async def execute_wave(opts: WaveExecutorOptions) -> WaveResult:
    """Execute a complete wave: foundation -> features -> merge -> integration."""
    wave = opts.wave
    foundation_results: list[TaskResult] = []
    feature_results: list[FeatureResult] = []
    integration_results: list[TaskResult] = []

    # Single semaphore shared across ALL phases (foundation, features,
    # integration) so that ``max_concurrency`` is a true system-wide
    # ceiling — not per-phase or per-feature.
    shared_sem = asyncio.Semaphore(opts.max_concurrency)

    async def run_task_with_runner(
        task: Task, phase: str
    ) -> TaskResult:
        """Run a single task using the configured runner."""
        # Skip already-completed tasks
        if task.id in opts.skip_task_ids:
            skipped = TaskResult(
                id=task.id,
                title=task.title,
                agent=task.agent,
                exit_code=0,
                output="Resumed — already completed in previous run",
                stderr="",
                duration_ms=0,
            )
            await _call(opts.on_task_start, phase, task)
            await _call(opts.on_task_end, phase, task, skipped)
            return skipped

        await _call(opts.on_task_start, phase, task)

        start = time.monotonic()

        # Build prompt for the agent
        prompt = _build_task_prompt(task, opts.spec_content, opts.data_schemas, opts.project_structure, opts.environment, opts.project_context)

        from wave_server.engine.types import RunnerConfig

        # Resolve model: agent-specific override > execution default > server default
        task_model = (
            (opts.agent_models or {}).get(task.agent)
            or opts.model
        ) or None

        config = RunnerConfig(
            task_id=task.id,
            prompt=prompt,
            cwd=opts.cwd,
            env=opts.env,
            model=task_model,
        )

        runner_result = await opts.runner.spawn(config)
        elapsed_ms = int((time.monotonic() - start) * 1000)

        output = opts.runner.extract_final_output(runner_result.stdout)

        result = TaskResult(
            id=task.id,
            title=task.title,
            agent=task.agent,
            exit_code=runner_result.exit_code,
            output=output if not runner_result.timed_out else f"Task timed out\n{output}",
            stderr=runner_result.stderr,
            duration_ms=elapsed_ms,
            stdout=runner_result.stdout,
            timed_out=runner_result.timed_out,
        )

        # Per-task commit for foundation/integration tasks (only on work branches)
        if result.exit_code == 0 and opts.repo_root:
            committed = await commit_task_output(
                opts.cwd, task.id, task.title, task.agent
            )
            if committed:
                await _call(
                    opts.on_log,
                    f"   📌 Committed: {task.id} [{task.agent}] — {task.title}",
                )

        await _call(opts.on_task_end, phase, task, result)

        return result

    # ── 1. Foundation Phase ─────────────────────────────────────

    if wave.foundation:
        await _call(
            opts.on_progress,
            ProgressUpdate(
                phase="foundation",
                current_tasks=[{"id": t.id, "status": "pending"} for t in wave.foundation],
            ),
        )
        await _call(opts.on_log, "### Foundation")

        f_results = await execute_dag(
            wave.foundation,
            lambda task: run_task_with_runner(task, "foundation"),
            opts.max_concurrency,
            semaphore=shared_sem,
        )
        foundation_results.extend(f_results)

        if any(r.exit_code != 0 for r in f_results):
            await _call(opts.on_log, "Foundation FAILED — skipping features and integration")
            return WaveResult(
                wave=wave.name,
                foundation_results=foundation_results,
                feature_results=feature_results,
                integration_results=integration_results,
                passed=False,
            )

    # ── 2. Feature Phase ───────────────────────────────────────

    if wave.features:
        await _call(
            opts.on_progress,
            ProgressUpdate(
                phase="features",
                features=[{"name": f.name, "status": "pending"} for f in wave.features],
            ),
        )
        await _call(opts.on_log, "### Features")

        # Single feature with name "default" → no git isolation needed
        is_single_default = len(wave.features) == 1 and wave.features[0].name == "default"

        # Determine if we should use git worktree isolation
        repo_root = opts.repo_root or opts.cwd
        use_git = opts.use_worktrees and not is_single_default
        if use_git:
            use_git = await is_git_repo(repo_root)

        # Create feature worktrees
        all_feature_worktrees: list[FeatureWorktree] = []
        feature_worktree_map: dict[str, FeatureWorktree | None] = {}

        if use_git:
            for feature in wave.features:
                wt = await create_feature_worktree(repo_root, opts.wave_num, feature.name)
                feature_worktree_map[feature.name] = wt
                if wt:
                    all_feature_worktrees.append(wt)

        async def run_feature(feature: Feature, idx: int) -> FeatureResult:
            feature_wt = feature_worktree_map.get(feature.name)

            result = await _execute_feature(
                feature=feature,
                runner=opts.runner,
                spec_content=opts.spec_content,
                data_schemas=opts.data_schemas,
                project_structure=opts.project_structure,
                environment=opts.environment,
                project_context=opts.project_context,
                cwd=opts.cwd,
                max_concurrency=opts.max_concurrency,
                skip_task_ids=opts.skip_task_ids,
                feature_worktree=feature_wt,
                wave_num=opts.wave_num,
                env=opts.env,
                auto_commit=opts.repo_root is not None,
                model=opts.model,
                agent_models=opts.agent_models,
                on_task_start=lambda task: _call(opts.on_task_start, f"feature:{feature.name}", task),
                on_task_end=lambda task, tr: _call(opts.on_task_end, f"feature:{feature.name}", task, tr),
                on_log=opts.on_log,
                semaphore=shared_sem,
            )

            return result

        # Run features in parallel if using git isolation, sequentially otherwise
        f_results = await map_concurrent(
            wave.features,
            len(wave.features) if use_git else 1,
            run_feature,
        )
        feature_results.extend(f_results)

        # ── 2b. Merge Phase ────────────────────────────────────

        if use_git and all_feature_worktrees:
            await _call(opts.on_progress, ProgressUpdate(phase="merge"))
            await _call(opts.on_log, "### Merge")

            merge_results = await merge_feature_branches(
                repo_root,
                all_feature_worktrees,
                [{"name": r.name, "passed": r.passed} for r in f_results],
                runner=opts.runner,
            )

            for mr in merge_results:
                await _call(opts.on_merge_result, mr)

            merge_conflicts = [m for m in merge_results if not m.success and m.had_changes]
            if merge_conflicts:
                await _call(opts.on_log, "Merge conflicts detected — skipping integration")
                return WaveResult(
                    wave=wave.name,
                    foundation_results=foundation_results,
                    feature_results=feature_results,
                    integration_results=integration_results,
                    passed=False,
                )

        if any(not r.passed for r in f_results):
            await _call(opts.on_log, "One or more features failed — skipping integration")
            # Emergency cleanup if worktrees remain
            if use_git and all_feature_worktrees:
                await cleanup_all(repo_root, all_feature_worktrees)
            return WaveResult(
                wave=wave.name,
                foundation_results=foundation_results,
                feature_results=feature_results,
                integration_results=integration_results,
                passed=False,
            )

    # ── 3. Integration Phase ───────────────────────────────────

    if wave.integration:
        await _call(
            opts.on_progress,
            ProgressUpdate(
                phase="integration",
                current_tasks=[{"id": t.id, "status": "pending"} for t in wave.integration],
            ),
        )
        await _call(opts.on_log, "### Integration")

        i_results = await execute_dag(
            wave.integration,
            lambda task: run_task_with_runner(task, "integration"),
            opts.max_concurrency,
            semaphore=shared_sem,
        )
        integration_results.extend(i_results)

    passed = (
        all(r.exit_code == 0 for r in foundation_results)
        and all(r.passed for r in feature_results)
        and all(r.exit_code == 0 for r in integration_results)
    )

    return WaveResult(
        wave=wave.name,
        foundation_results=foundation_results,
        feature_results=feature_results,
        integration_results=integration_results,
        passed=passed,
    )


_FRONTMATTER_RE = re.compile(r"\A---\s*\n.*?\n---\s*\n", re.DOTALL)

_FALLBACK_ROLES = {
    "wave-verifier": "You are verifying completed work.",
    "test-writer": "You are writing tests.",
}

_agent_role_cache: dict[str, str | None] = {}

log = logging.getLogger(__name__)


_BUILTIN_AGENTS_DIR = Path(__file__).resolve().parent.parent.parent / "agents"


def _load_agent_role(agent_name: str) -> str:
    """Load role instructions from an agent .md file.

    Resolution order:
      1. settings.agents_dir / {agent_name}.md  (user override)
      2. <wave-server-repo>/agents/{agent_name}.md  (built-in)
      3. Hardcoded fallback (one-liner)
    """
    if agent_name in _agent_role_cache:
        cached = _agent_role_cache[agent_name]
        if cached is not None:
            return cached
        return _FALLBACK_ROLES.get(agent_name, "You are implementing code.")

    from wave_server.config import settings

    candidates: list[Path] = []
    if settings.agents_dir:
        candidates.append(Path(settings.agents_dir) / f"{agent_name}.md")
    candidates.append(_BUILTIN_AGENTS_DIR / f"{agent_name}.md")

    for path in candidates:
        if path.is_file():
            try:
                content = path.read_text(encoding="utf-8")
                # Strip YAML frontmatter
                content = _FRONTMATTER_RE.sub("", content).strip()
                _agent_role_cache[agent_name] = content
                log.info("Loaded agent role from %s", path)
                return content
            except OSError:
                continue

    _agent_role_cache[agent_name] = None
    return _FALLBACK_ROLES.get(agent_name, "You are implementing code.")


def _build_task_prompt(task: Task, spec_content: str, data_schemas: str, project_structure: str = "", environment: str = "", project_context: str = "") -> str:
    """Build the prompt sent to the agent subprocess."""
    schemas_block = (
        f"\n## Data Schemas (authoritative — use these exact names)\n{data_schemas}\n"
        if data_schemas
        else ""
    )
    structure_block = f"\n## Project Structure\n{project_structure}\n" if project_structure else ""
    env_block = f"\n## Environment\n{environment}\n" if environment else ""
    legacy_ctx = f"\n{project_context}\n" if project_context else ""
    context_block = f"{structure_block}{env_block}{legacy_ctx}"

    role = _load_agent_role(task.agent)

    # Build task-specific context
    if task.agent == "wave-verifier":
        files_line = f"Files to check: {', '.join(task.files)}" if task.files else ""
    else:
        files_line = f"Files: {', '.join(task.files)}" if task.files else ""

    test_context = ""
    if task.agent not in ("wave-verifier", "test-writer") and task.test_files:
        test_context = f"\nTests to satisfy: {', '.join(task.test_files)}\nYour implementation MUST make these tests pass."

    return f"""{role}
{schemas_block}{context_block}
## Your Task
**{task.id}: {task.title}**
{files_line}{test_context}

{task.description}

- Work continuously — do NOT stop to summarize progress or wait for feedback"""
