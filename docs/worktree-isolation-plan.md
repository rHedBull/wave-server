# Git Worktree Isolation for Parallel Execution

## Current Flow (no isolation)

```
/projects/myapp/                    ← single directory
  checkout wave/exec-abc123         ← entire repo switches branch
  run foundation tasks              ← all in same dir
  run feature-A tasks               ← same dir
  run feature-B tasks               ← same dir (conflicts if parallel!)
  run integration tasks             ← same dir
  push, checkout original branch
```

All tasks — across all features — share one working directory. Features technically run via `map_concurrent` but they're writing to the same files, so it's a race condition if two features touch overlapping files.

## Target Flow (two tiers of worktree isolation)

```
/projects/myapp/                              ← stays on original branch, untouched
  .wave-worktrees/
    wave-1/auth/                              ← feature worktree (own checkout)
      agent runs auth tasks here
    wave-1/dashboard/                         ← feature worktree (own checkout)
      agent runs dashboard tasks here
      .wave-sub-worktrees/
        wave-1/dashboard--t3/                 ← sub-worktree for parallel task
        wave-1/dashboard--t4/                 ← sub-worktree for parallel task
```

Foundation and integration run in the main worktree (on the work branch). Each feature gets its own worktree branching from the post-foundation state. Within a feature, parallel tasks at the same DAG level get sub-worktrees branching from the feature branch.

After completion, merges cascade: sub-worktrees → feature branch → work branch.

---

## Changes Required

### 1. `wave_executor.py` — Feature phase creates worktrees

Currently `run_feature()` is an inline function that runs tasks sequentially in `opts.cwd`. It needs to:

```python
# Before running features:
repo_root = opts.cwd
worktrees: list[FeatureWorktree] = []

for feature in wave.features:
    wt = await create_feature_worktree(repo_root, opts.wave_num, feature.name)
    if wt:
        worktrees.append(wt)
    else:
        # Fallback: run without isolation (single feature or non-git)
        worktrees.append(None)

# run_feature now receives a worktree and runs tasks in wt.dir instead of opts.cwd
async def run_feature(feature, idx):
    wt = worktrees[idx]
    cwd = wt.dir if wt else opts.cwd
    # ... run tasks in cwd ...
```

After all features complete:

```python
# Merge successful feature branches back into the work branch
merge_results = await merge_feature_branches(repo_root, worktrees, feature_results)
# Emit merge events
# Cleanup worktrees
await cleanup_worktrees(repo_root, worktrees)
```

The key change: each feature's agent subprocess gets `cwd=wt.dir` instead of `cwd=opts.cwd`.

### 2. `feature_executor.py` — Parallel tasks within a feature use sub-worktrees

Currently `execute_feature()` runs tasks through the DAG but all in the same `cwd`. For parallel tasks (same DAG level), it needs sub-worktrees:

```python
async def execute_feature(feature, runner, cwd, ...):
    # When the DAG has parallel tasks at a level:
    parallel_task_ids = [t.id for t in level_tasks]
    
    if len(parallel_task_ids) > 1 and is_git:
        # Create sub-worktrees branching from the feature branch
        sub_wts = await create_sub_worktrees(feature_wt, wave_num, parallel_task_ids)
        # Run each task in its own sub-worktree directory
        for task, sub_wt in zip(level_tasks, sub_wts):
            config.cwd = sub_wt.dir
            await runner.spawn(config)
        # Merge sub-worktrees back into feature branch
        await merge_sub_worktrees(feature_wt, sub_wts, results)
    else:
        # Sequential: run directly in the feature worktree (no sub needed)
        await runner.spawn(config)
```

This requires the DAG executor to expose level-by-level execution rather than the current black-box `execute_dag()`.

### 3. `git_worktree.py` — Add sub-worktree helpers

The file already has `create_feature_worktree`, `merge_feature_branches`, and `cleanup_worktrees`. What's missing:

- **`create_sub_worktrees(feature_wt, wave_num, task_ids)`** — creates per-task worktrees branching from the feature branch
- **`merge_sub_worktrees(feature_wt, sub_wts, results)`** — merges successful sub-worktree branches back into the feature branch, with conflict resolution
- **`cleanup_sub_worktrees()`** — removes sub-worktree directories and prunes branches
- **Async conflict resolution** — the client-side spawns a `wave-doctor` agent to resolve merge conflicts; the server needs the same (spawn a runner with a conflict-resolution prompt)

### 4. `execution_manager.py` — Stop switching branches on the main checkout

Currently it does `checkout_branch(repo_cwd, work_branch)` which switches the whole repo. With worktrees, the main checkout stays untouched:

```python
# Instead of:
await create_work_branch(repo_cwd, work_branch, start_point)  # switches the repo

# Do:
# Create work branch (just the ref, don't checkout)
await _run_git(["branch", work_branch, start_point], repo_cwd)
# Feature worktrees will be created by the wave executor
# No need to restore original_branch at the end — it was never changed
```

This means `original_branch` tracking and the restore-on-failure logic can be simplified — the main directory is never touched.

### 5. `dag.py` — Expose level-based execution

Currently `execute_dag` runs everything and handles parallelism internally. For sub-worktree support, you need to know which tasks are at the same level (can run in parallel) vs which are sequential. Either:

- Expose `build_dag()` levels and iterate manually in the feature executor
- Or add a callback hook to `execute_dag` that's called per-level with the parallel tasks, so the feature executor can create/merge sub-worktrees at level boundaries

### 6. Prompt changes

Agents running in worktrees need to know they're in an isolated directory:

```python
# Add to task prompt when running in a worktree:
"You are working in an isolated git worktree. Use relative paths only. "
"Do NOT run git checkout or git branch commands."
```

The client-side pi-wave extension already does this (see `feature-executor.ts` line ~297).

### 7. `WaveExecutorOptions` — Add worktree config

```python
@dataclass
class WaveExecutorOptions:
    # ... existing fields ...
    repo_root: str | None = None    # separate from cwd for worktree creation
    use_worktrees: bool = True       # can disable for non-git or testing
```

---

## Files Touched

| File | Change |
|------|--------|
| `wave_executor.py` | Create feature worktrees before feature phase, merge after, pass `wt.dir` as cwd |
| `feature_executor.py` | Create sub-worktrees for parallel DAG levels, merge back after each level |
| `git_worktree.py` | Add `create_sub_worktrees`, `merge_sub_worktrees`, async conflict resolution |
| `execution_manager.py` | Stop checking out the work branch on the main repo; let worktrees handle it |
| `dag.py` | Expose level-by-level iteration for sub-worktree creation |
| `types.py` | Already has `FeatureWorktree`, `SubWorktree`, `MergeResult` — mostly ready |

## What's NOT Needed

- No new dependencies
- No Docker, no bubblewrap
- No schema/model changes (worktrees are ephemeral, not persisted)
- Tests mostly work as-is since the mock runner doesn't actually write files

## Biggest Complexity: Merge Conflict Resolution

When two parallel feature branches both modify overlapping files, you need either an agent to resolve it or a clear failure path.

The client-side pi-wave extension handles this by spawning a `wave-doctor` agent (see `git-worktree.ts` `tryResolveConflicts`). The server would do the same: spawn the runner with a conflict-resolution prompt. If the agent can't resolve it, abort the merge and preserve the branch for manual resolution.

## Reference Implementation

The full worktree logic already exists in the pi-wave client-side extension:

- `extensions/subagent/git-worktree.ts` — all git worktree operations, merge, conflict resolution
- `extensions/wave-executor/wave-executor.ts` — feature worktree creation/cleanup
- `extensions/wave-executor/feature-executor.ts` — sub-worktree creation for parallel tasks
