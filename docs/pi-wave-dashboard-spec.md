# Spec: pi-wave-dashboard

## Overview

A standalone project workspace for monitoring and interacting with pi-wave-workflow executions in real-time. Users log in via GitHub, register projects linked to GitHub repos, and track wave sequences (spec → plan → execution) through a live dashboard. Built as a Next.js app on Vercel with Supabase for data/auth/realtime and Cloudflare R2 for blob storage.

## Current State

The wave executor (`pi-wave-workflow/extensions/wave-executor/`) runs locally inside pi. It has a rich callback system:

| Callback | Fires when |
|---|---|
| `onProgress(update)` | Phase changes, feature status updates |
| `onTaskStart(phase, task)` | Task begins execution |
| `onTaskEnd(phase, task, result)` | Task completes (pass, fail, timeout, skip) |
| `onFixCycleStart(phase, task)` | Verifier failed → fix agent launched |
| `onStallRetry(phase, task, reason)` | Stall detected → retry with guidance |
| `onMergeResult(result)` | Feature branch merged (or conflicted) |
| `onLog(line)` | Freeform log line |

Existing types (`types.ts`): `Task`, `TaskResult`, `FeatureResult`, `WaveResult`, `MergeResult`, `ProgressUpdate`, `Plan`, `Wave`, `Feature`.

The `/waves-spec` command produces a SPEC.md, `/waves-plan` produces a PLAN.md, and `/waves-execute` runs it. These artifacts live on disk at `docs/spec/` and `docs/plan/` inside the repo.

Current state is only visible in the pi TUI widget and a log file. No remote access, no persistence across sessions, no interactive blocker resolution, no cross-project view.

## Architecture

```
┌─────────────┐   POST /api/events     ┌─────────────────────────────────┐
│ Wave Executor│ ──────────────────────→│  Vercel                          │
│ (pi extension)│  (batched, every 2s)  │  Next.js App                     │
└─────────────┘                        │  ├── Pages (SSR/static)          │
       │                                │  └── API Routes (serverless)     │
       │  GET /api/commands/poll        └──────┬──────────┬───────────────┘
       │  (short-poll, 5s interval)            │          │
       │←──────────────────────────────────────┘          │
                                                          │ writes/reads
                                          ┌───────────────┴───────────────┐
                                          │                               │
                                   ┌──────┴───────┐              ┌───────┴────────┐
                                   │  Supabase     │              │  Cloudflare R2  │
                                   │  ├── Postgres │              │  (S3 bucket)    │
                                   │  ├── Auth     │              │                 │
                                   │  └── Realtime │              │  Specs, plans,  │
                                   │               │              │  task output,   │
                                   │  Metadata,    │              │  execution logs │
                                   │  events,      │              └─────────────────┘
                                   │  commands     │
                                   └──────┬────────┘
                                          │ Realtime
                                          ▼ subscriptions
                                   ┌─────────────┐
                                   │  Browser UI  │
                                   └─────────────┘
```

### What lives where

| Store | Contents | Why |
|---|---|---|
| **Supabase Postgres** | Projects, sequences, executions (metadata), events (append-only), commands, user sessions | Structured data, Realtime subscriptions, queries, RLS |
| **Cloudflare R2** | Spec markdown, plan markdown, task output blobs, execution logs | Large text blobs, no egress fees, keeps Postgres lean |
| **Vercel** | Next.js app (pages + API routes) | Serverless hosting, zero config deploys |

### Free tier budgets

| Service | Limit | Expected usage |
|---|---|---|
| **Vercel Hobby** | 100k function invocations/month, 10s timeout, 100 GB bandwidth | ~5-10k invocations/month (light use) |
| **Supabase Free** | 500 MB Postgres, unlimited API, Realtime included, 50k MAU | ~50-100 MB (structured metadata + events) |
| **Cloudflare R2 Free** | 10 GB storage, 10M class A ops, 10M class B ops, zero egress | ~1-2 GB (specs, plans, output blobs) |

## Data Model

### Hierarchy

```
User (GitHub identity)
└── Project (linked to 1+ GitHub repos)
    └── Wave Sequence (one workflow lifecycle, e.g., "add-oauth")
        ├── Spec (markdown in R2)
        ├── Plan (markdown in R2)
        └── Execution[] (one or more runs of this plan)
            ├── Events[] (append-only in Postgres, Realtime-subscribed)
            ├── Task output blobs (in R2)
            └── Commands[] (blocker resolution)
```

### Supabase Tables

```sql
-- Auth handled by Supabase Auth (GitHub provider) — no custom users table needed.
-- supabase.auth.users provides id, email, user_metadata (GitHub avatar, login, etc.)

create table projects (
  id            uuid primary key default gen_random_uuid(),
  user_id       uuid references auth.users(id) on delete cascade not null,
  name          text not null,
  description   text,
  api_key       text unique not null default encode(gen_random_bytes(32), 'hex'),
  created_at    timestamptz default now(),
  updated_at    timestamptz default now()
);

create table project_repos (
  id            uuid primary key default gen_random_uuid(),
  project_id    uuid references projects(id) on delete cascade not null,
  owner         text not null,
  repo          text not null,
  unique(project_id, owner, repo)
);

create table sequences (
  id            uuid primary key default gen_random_uuid(),
  project_id    uuid references projects(id) on delete cascade not null,
  name          text not null,                          -- slug, e.g. "add-oauth"
  description   text,
  status        text not null default 'drafting',       -- drafting | planned | executing | completed | failed
  spec_key      text,                                   -- R2 object key for spec markdown
  plan_key      text,                                   -- R2 object key for plan markdown
  wave_count    int,
  task_count    int,
  created_at    timestamptz default now(),
  updated_at    timestamptz default now()
);

create table executions (
  id              uuid primary key default gen_random_uuid(),
  sequence_id     uuid references sequences(id) on delete cascade not null,
  status          text not null default 'running',      -- running | completed | failed
  trigger         text not null default 'initial',      -- initial | retry | continuation
  total_tasks     int not null default 0,
  completed_tasks int not null default 0,
  waves_state     jsonb not null default '[]',          -- compact: wave names, statuses, phases
  started_at      timestamptz default now(),
  finished_at     timestamptz
);

create table events (
  id              uuid primary key default gen_random_uuid(),
  execution_id    uuid references executions(id) on delete cascade not null,
  event_type      text not null,
  payload         jsonb not null default '{}',
  created_at      timestamptz default now()
);

create table commands (
  id              uuid primary key default gen_random_uuid(),
  execution_id    uuid references executions(id) on delete cascade not null,
  task_id         text not null,
  action          text,                                 -- null until user decides; then "retry" | "skip"
  message         text,
  picked_up       boolean not null default false,
  created_at      timestamptz default now(),
  resolved_at     timestamptz
);

-- Indexes
create index idx_events_execution on events(execution_id, created_at);
create index idx_commands_poll on commands(execution_id, task_id, picked_up) where not picked_up;
create index idx_sequences_project on sequences(project_id, created_at desc);
create index idx_executions_sequence on executions(sequence_id, started_at desc);
```

### Row-Level Security

```sql
-- Projects: users see only their own
alter table projects enable row level security;
create policy "users own projects" on projects
  for all using (auth.uid() = user_id);

-- Cascade: sequences/executions/events/commands visible if user owns the project
-- (join through project_id or sequence_id → project_id)
-- API key access for executor: validated in API routes, bypasses RLS via service role
```

### R2 Object Keys

```
specs/{sequence_id}/spec.md
plans/{sequence_id}/plan.md
output/{execution_id}/{task_id}.txt
logs/{execution_id}/log.txt
```

## Requirements

### Auth

- **FR-1**: Users authenticate via Supabase Auth with GitHub as the OAuth provider. Login redirects to GitHub, callback handled by Supabase, session stored as a cookie.
- **FR-2**: The executor authenticates via a per-project API key (`X-API-Key` header). API routes validate the key against the `projects.api_key` column using the Supabase service role client (bypasses RLS).
- **FR-3**: API key auth is optional when the request comes from localhost. Required for remote requests.

### Projects

- **FR-4**: Create a project with name, optional description, and optional GitHub repo links. Returns the project with its generated API key (shown once in the UI).
- **FR-5**: List all projects for the logged-in user, ordered by most recently updated.
- **FR-6**: Update project name, description, add/remove linked repos.
- **FR-7**: Delete a project — cascades to all sequences, executions, events, commands, and R2 objects.
- **FR-8**: Regenerate a project's API key. Invalidates the old key immediately.
- **FR-9**: Link GitHub repos to a project. The UI shows a repo picker using the user's GitHub token (from Supabase Auth) to list accessible repos.

### Wave Sequences

- **FR-10**: Create a wave sequence under a project. The executor calls this when `/waves-spec` starts.
- **FR-11**: Upload/update the spec markdown to R2 via a presigned URL or API route proxy. The executor calls this when the spec file is written.
- **FR-12**: Upload/update the plan markdown to R2. The executor calls this when `/waves-plan` completes.
- **FR-13**: List sequences for a project with status and summary info.
- **FR-14**: Get sequence detail including spec and plan content (fetched from R2).
- **FR-15**: Sequence status is derived from its children: `drafting` (has spec, no plan), `planned` (has plan, no executions), `executing` (has a running execution), `completed` (latest execution passed), `failed` (latest execution failed).

### Executions

- **FR-16**: Create an execution under a sequence. The executor calls this when `/waves-execute` starts. Payload: trigger type, total task count, wave names.
- **FR-17**: Push events to an execution. The API route inserts into the `events` table and updates `executions.waves_state` / `completed_tasks`. Supabase Realtime broadcasts the insert to subscribed browsers.
- **FR-18**: Get execution detail — metadata + waves_state. Full event history fetched separately (paginated if needed).
- **FR-19**: List executions for a sequence, ordered by start time.

### Event Batching

- **FR-20**: The executor client buffers events and flushes every 2 seconds (or immediately if the buffer exceeds 20 events). Each flush is a single `POST /api/executions/{id}/events` with an array of events.
- **FR-21**: The API route inserts all events in one batch, updates execution state once, and Supabase Realtime picks up the inserts.
- **FR-22**: Log events (`onLog` lines) are buffered separately and flushed as a single `log_batch` event to reduce row count. Additionally, the full log is appended to an R2 object (`logs/{execution_id}/log.txt`) periodically.

### Blocker Resolution

- **FR-23**: When a task fails and blocker resolution is enabled, the executor posts a `blocker` event. The API route inserts a row into `commands` with `action=null`.
- **FR-24**: The executor short-polls `GET /api/executions/{id}/commands/poll?task={taskId}` every 5 seconds, up to a configurable timeout (default: 5 minutes). The API route checks for a command row where `picked_up=false` and `action` is not null.
- **FR-25**: The browser UI shows retry/skip buttons. Clicking calls `POST /api/executions/{id}/commands/{commandId}` with `{ action: "retry" | "skip" }`, which updates the command row.
- **FR-26**: The executor's next poll picks up the resolved command, marks it `picked_up=true`, and acts. If timeout expires, executor falls back to default behavior (auto-retry once, then fail).

### Real-time Updates

- **FR-27**: The browser subscribes to Supabase Realtime on the `events` table, filtered by `execution_id`. New event inserts are pushed to the browser automatically.
- **FR-28**: The browser also subscribes to `executions` row changes (status, completed_tasks, waves_state) for summary updates.
- **FR-29**: On the project overview page, subscribe to `executions` filtered by sequence IDs belonging to that project — shows live activity across sequences.

### Browser UI

- **FR-30**: **Left sidebar** — project list. Each shows name, linked repo count, status indicator (idle / has running execution). Click to select. Collapsible.
- **FR-31**: **Project overview** (main area when project selected):
  - Linked GitHub repos with links to GitHub.
  - Wave sequences as cards: name, status badge, task progress, timestamps.
  - Active executions highlighted at the top.
  - Settings gear → project settings page.
- **FR-32**: **Project settings page**:
  - Edit name/description.
  - Manage linked repos (add/remove via repo picker).
  - API key display (masked, copy button, regenerate).
- **FR-33**: **Sequence detail page**:
  - Spec content rendered as markdown (collapsible section).
  - Plan content rendered as markdown (collapsible section).
  - Execution history — list of executions with status, duration, task count. Click to open.
- **FR-34**: **Execution detail page** (real-time):
  - **Wave timeline** — horizontal progress: waves as stages, current wave highlighted, sub-phases (foundation → features → merge → integration).
  - **Feature swim lanes** — parallel features as columns within the current wave.
  - **Task cards** — status icon, agent badge (🧪/🔨/🔍), title, live elapsed time, expandable output (fetched from R2 on expand).
  - **Log stream** — scrollable live tail (from Realtime event subscription).
  - **Blocker banner** — prominent retry/skip UI when a task is blocked.
- **FR-35**: Task cards show visual indicators: fix cycle (🔧), stall retry (🔄), timed out (⏰), skipped (⏭).
- **FR-36**: Merge results displayed between features and integration phases.

## Event Types

```typescript
type EventType =
  | "run_started"
  | "phase_changed"
  | "task_started"
  | "task_completed"
  | "task_failed"
  | "feature_started"
  | "feature_completed"
  | "merge_result"
  | "stall_detected"
  | "fix_cycle_started"
  | "blocker"
  | "blocker_resolved"
  | "log_batch"           // batched log lines (array of strings)
  | "run_completed";
```

## API Routes

```
# Auth (Supabase handles OAuth, these are helpers)
GET    /api/auth/callback             # Supabase OAuth callback handler
POST   /api/auth/logout               # Clear session

# Projects
POST   /api/projects                  # Create project
GET    /api/projects                  # List user's projects
GET    /api/projects/[id]             # Get project
PATCH  /api/projects/[id]             # Update project
DELETE /api/projects/[id]             # Delete + cascade
POST   /api/projects/[id]/api-key     # Regenerate API key

# Repos
GET    /api/github/repos              # List user's accessible GitHub repos
POST   /api/projects/[id]/repos       # Link a repo
DELETE /api/projects/[id]/repos/[repoId]  # Unlink a repo

# Sequences
POST   /api/projects/[id]/sequences   # Create sequence
GET    /api/projects/[id]/sequences   # List sequences
GET    /api/sequences/[id]            # Get sequence detail
POST   /api/sequences/[id]/spec       # Upload spec to R2
POST   /api/sequences/[id]/plan       # Upload plan to R2
GET    /api/sequences/[id]/spec       # Get spec content (proxy from R2)
GET    /api/sequences/[id]/plan       # Get plan content (proxy from R2)

# Executions
POST   /api/sequences/[id]/executions         # Create execution
GET    /api/sequences/[id]/executions         # List executions
GET    /api/executions/[id]                   # Get execution detail
POST   /api/executions/[id]/events            # Push event batch (executor)
GET    /api/executions/[id]/events            # List events (paginated)

# Blocker commands
GET    /api/executions/[id]/commands/poll     # Executor short-polls (?task=X)
POST   /api/executions/[id]/commands/[cmdId]  # Browser resolves (retry/skip)

# Task output
GET    /api/executions/[id]/output/[taskId]   # Fetch task output from R2

# Auth: executor routes use X-API-Key, browser routes use Supabase session cookie
```

## Callback → Event Mapping

| Executor hook | Dashboard action |
|---|---|
| `/waves-spec` starts | `POST /api/projects/{id}/sequences` (create sequence) |
| Spec file written | `POST /api/sequences/{id}/spec` (upload to R2) |
| `/waves-plan` completes | `POST /api/sequences/{id}/plan` (upload to R2) |
| `/waves-execute` starts | `POST /api/sequences/{id}/executions` (create execution) |
| `onProgress({ phase })` | Buffer `phase_changed` event |
| `onProgress({ features })` | Buffer `feature_started` / `feature_completed` |
| `onTaskStart(phase, task)` | Buffer `task_started` event |
| `onTaskEnd` (exitCode 0) | Buffer `task_completed` + upload output to R2 |
| `onTaskEnd` (exitCode ≠ 0) | Buffer `task_failed` → flush → `blocker` if enabled |
| `onFixCycleStart` | Buffer `fix_cycle_started` |
| `onStallRetry` | Buffer `stall_detected` |
| `onMergeResult` | Buffer `merge_result` |
| `onLog(line)` | Append to log buffer → flush as `log_batch` |
| All waves done | Flush → `run_completed` event |

## Repo Structure

```
pi-wave-dashboard/
├── src/
│   ├── app/
│   │   ├── layout.tsx                                    # Shell: sidebar + main area
│   │   ├── page.tsx                                      # Landing / redirect to projects
│   │   ├── login/page.tsx                                # GitHub login trigger
│   │   ├── auth/callback/route.ts                        # Supabase OAuth callback
│   │   └── projects/
│   │       ├── page.tsx                                  # Projects list (fallback)
│   │       └── [projectId]/
│   │           ├── page.tsx                              # Project overview
│   │           ├── settings/page.tsx                     # Project settings, repos, API key
│   │           └── sequences/
│   │               └── [seqId]/
│   │                   ├── page.tsx                      # Sequence detail
│   │                   └── executions/
│   │                       └── [execId]/page.tsx         # Live execution view
│   ├── components/
│   │   ├── Sidebar.tsx
│   │   ├── ProjectCard.tsx
│   │   ├── SequenceCard.tsx
│   │   ├── ExecutionCard.tsx
│   │   ├── WaveTimeline.tsx
│   │   ├── TaskCard.tsx
│   │   ├── FeatureLane.tsx
│   │   ├── LogStream.tsx
│   │   ├── BlockerBanner.tsx
│   │   ├── MarkdownView.tsx
│   │   └── RepoLinker.tsx
│   ├── hooks/
│   │   ├── useExecutionStream.ts                        # Supabase Realtime for events
│   │   ├── useProjectActivity.ts                        # Supabase Realtime for project
│   │   └── useSupabase.ts                               # Supabase client singleton
│   └── lib/
│       ├── supabase/
│       │   ├── client.ts                                # Browser client
│       │   ├── server.ts                                # Server client (API routes)
│       │   └── service.ts                               # Service role client (executor auth)
│       ├── r2.ts                                        # R2 client (S3-compatible SDK)
│       └── api.ts                                       # Executor-facing API helpers
│
├── supabase/
│   └── migrations/
│       └── 001_initial.sql                              # Tables, RLS, indexes
│
├── package.json
├── next.config.ts
├── .env.example                                          # SUPABASE_URL, SUPABASE_ANON_KEY,
│                                                         # SUPABASE_SERVICE_ROLE_KEY,
│                                                         # R2_ACCOUNT_ID, R2_ACCESS_KEY_ID,
│                                                         # R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME,
│                                                         # GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET
└── README.md
```

## Affected Files (in pi-wave-workflow)

- `extensions/wave-executor/dashboard-client.ts` — **new file**. HTTP client with event buffering (2s / 20 events flush), R2 upload for output/specs/plans, sequence/execution lifecycle calls. Fire-and-forget — errors silently dropped.
- `extensions/wave-executor/index.ts` — **modify**. Hook dashboard client into `/waves-spec`, `/waves-plan`, `/waves-execute`. Compose dashboard callbacks with TUI callbacks. Add blocker short-poll loop.
- `extensions/wave-executor/types.ts` — **modify**. Add `DashboardConfig` interface (`url`, `apiKey`, `projectId`, `blockerTimeoutMs?`, `batchIntervalMs?`).

## Request Budget

With event batching (2s flush), a typical 18-task execution:

| Phase | Requests | Notes |
|---|---|---|
| Create sequence | 1 | Once per spec |
| Upload spec | 1 | R2 PUT |
| Upload plan | 1 | R2 PUT |
| Create execution | 1 | |
| Event batches | ~10-15 | ~60 lifecycle events + ~50 log lines, batched into 2s windows |
| Task output uploads | ~18 | R2 PUT per task |
| Blocker polls | 0-60 | Only on failures, 5s interval × timeout |
| **Total API route calls** | **~15-20** | Well within Vercel limits |
| **Total R2 operations** | **~20** | Negligible |

10 executions/day × 30 days = ~6,000 API calls + ~6,000 R2 ops/month. Both well under free tier limits.

## Testing Criteria

### API Routes

- Supabase Auth: GitHub login → session → /api/projects returns user's projects.
- Project CRUD: create, list, update, delete with cascading (including R2 cleanup).
- API key auth: remote request without key → 401; with valid key → 200; localhost → 200.
- Sequence lifecycle: create → upload spec (R2) → upload plan (R2) → status transitions correct.
- Execution events: POST batch → rows inserted in events table → execution state updated.
- Supabase Realtime: event insert triggers subscription callback in browser client.
- Command flow: create blocker → poll returns null → browser resolves → poll returns action.
- R2 operations: upload spec/plan/output, retrieve, delete on project deletion.

### UI

- Sidebar lists projects with status indicators; updates on Realtime subscription.
- Project overview shows sequences as cards; active executions highlighted.
- Sequence detail renders spec/plan markdown from R2; lists executions.
- Execution detail connects via Supabase Realtime and updates live.
- Task cards show correct icons, live elapsed time; expand fetches output from R2.
- Blocker banner appears, retry/skip sends command and UI reflects resolution.
- Repo linker fetches and displays repos from GitHub API.

### Executor Client

- Creates sequence on `/waves-spec`, uploads spec on file write.
- Uploads plan on `/waves-plan` completion.
- Creates execution on `/waves-execute`, buffers and flushes events.
- Uploads task output to R2 on task completion.
- When dashboard unreachable, all operations silently fail — execution unaffected.
- Blocker poll: retry → re-execute, skip → mark skipped, timeout → default behavior.

## Out of Scope (v2+)

- Chat with running agents from the browser.
- Quick edit / patch injection from the dashboard.
- Multi-user / permissions / RBAC (v1 is single-user).
- Notifications (email, Slack, webhooks).
- Historical analytics / run comparisons.
- Log search / filtering.
- Plan editing from the dashboard.
- Dashboard cloning/reading repo contents from GitHub.
- Cross-sequence dependencies.
