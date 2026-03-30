# API Reference

All endpoints are prefixed with `/api`. No authentication required for v1 (localhost). See [Architecture](architecture.md) for the system overview and data model behind these endpoints.

## Health

```
GET /api/health
```

Response:
```json
{ "status": "ok", "active_executions": 0 }
```

## Projects

### Create project

```
POST /api/projects
Content-Type: application/json

{ "name": "my-project", "description": "optional" }
```

Returns `201` with the project including auto-generated `api_key`.

### List projects

```
GET /api/projects
```

Returns all projects, ordered by `created_at` descending.

### Get project

```
GET /api/projects/{project_id}
```

### Update project

```
PATCH /api/projects/{project_id}
Content-Type: application/json

{ "name": "new name", "description": "new desc" }
```

All fields optional — only provided fields are updated.

### Delete project

```
DELETE /api/projects/{project_id}
```

Returns `204`. Cascades: deletes all sequences, executions, events, and commands.

### Regenerate API key

```
POST /api/projects/{project_id}/regenerate-key
```

Returns the project with a new `api_key`. Old key is invalidated.

## Sequences

### Create sequence

```
POST /api/projects/{project_id}/sequences
Content-Type: application/json

{ "name": "add-oauth", "description": "optional" }
```

Returns `201`.

### List sequences

```
GET /api/projects/{project_id}/sequences
```

Returns sequences for the project, ordered by `created_at` descending.

### Get sequence

```
GET /api/sequences/{sequence_id}
```

### Update sequence

```
PATCH /api/sequences/{sequence_id}
Content-Type: application/json

{ "name": "new name", "status": "planned" }
```

### Upload spec

```
POST /api/sequences/{sequence_id}/spec
Content-Type: text/plain

<raw markdown body>
```

Returns `204`. Writes spec to `storage/specs/{sequence_id}/spec.md`.

### Get spec

```
GET /api/sequences/{sequence_id}/spec
```

Returns `text/plain` with the spec markdown content.

### Upload plan

```
POST /api/sequences/{sequence_id}/plan
Content-Type: text/plain

<raw markdown body>
```

Returns `204`. Writes plan to `storage/plans/{sequence_id}/plan.md`.

### Get plan

```
GET /api/sequences/{sequence_id}/plan
```

Returns `text/plain` with the plan markdown content.

## Executions

### Create execution

```
POST /api/sequences/{sequence_id}/executions
Content-Type: application/json

{
  "runtime": "claude",
  "concurrency": 4,
  "timeout_ms": 300000
}
```

All fields optional. Creates the execution (status=queued) and immediately launches background execution. Returns `201` with the execution.

### List executions

```
GET /api/sequences/{sequence_id}/executions
```

### Get execution

```
GET /api/executions/{execution_id}
```

Response includes `status`, `total_tasks`, `completed_tasks`, `current_wave`, `waves_state` (JSON).

### Cancel execution

```
POST /api/executions/{execution_id}/cancel
```

Returns `204`. Only works when status is `queued` or `running`.

### Continue execution

```
POST /api/executions/{execution_id}/continue
```

Creates a new execution (trigger=continuation) that resumes from the failure point. Only works when status is `failed` or `cancelled`. Returns `201` with the new execution.

### List events

```
GET /api/executions/{execution_id}/events?since=2024-01-01T00:00:00Z&limit=50
```

Returns events ordered by `created_at`. `since` filters to events after the timestamp. `limit` max 200, default 50.

Event types: `run_started`, `phase_changed`, `task_started`, `task_completed`, `task_failed`, `task_skipped`, `wave_completed`, `run_completed`.

### Task summary

```
GET /api/executions/{execution_id}/tasks
```

Returns a list of tasks with current status, built from events:

```json
[
  { "task_id": "1a", "status": "completed", "phase": "foundation", ... },
  { "task_id": "2a", "status": "running", "phase": "feature:auth", ... }
]
```

### Get task output

```
GET /api/executions/{execution_id}/output/{task_id}
```

Returns `text/plain` with the task's output.

### Get execution log

```
GET /api/executions/{execution_id}/log
```

Returns `text/plain` with the full execution log.

## Blockers

### List unresolved blockers

```
GET /api/executions/{execution_id}/blockers
```

Returns commands where `picked_up=false`.

### Resolve blocker

```
POST /api/executions/{execution_id}/blockers/{command_id}
Content-Type: application/json

{ "action": "retry", "message": "optional note" }
```

`action` must be `"retry"` or `"skip"`. Returns the updated command.

## Promote

### Promote execution

```
POST /api/v1/executions/{execution_id}/promote
Content-Type: application/json

{
  "promotion_target": "main",
  "merge_method": "squash"
}
```

Both fields are optional. Defaults: `promotion_target="main"`, `merge_method="squash"`.

Uses the bot account PAT to:
1. Approve the execution's PR
2. Merge it into the target branch
3. Create a promotion PR to the promotion target (default: `main`)

Returns:
```json
{
  "success": true,
  "merged_pr_url": "https://github.com/owner/repo/pull/25",
  "promotion_pr_url": "https://github.com/owner/repo/pull/26",
  "error": null
}
```

Requires `WAVE_GITHUB_TOKEN` to be configured. See [GitHub Authentication](github-auth.md) for setup details.
