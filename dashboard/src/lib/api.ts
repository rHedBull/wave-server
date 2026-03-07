const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api/v1";
const API_BASE = process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8000/api";

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...options?.headers,
    },
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status}: ${text}`);
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}

// --- Types ---

export interface Project {
  id: string;
  name: string;
  description: string | null;
  api_key: string;
  created_at: string;
  updated_at: string;
}

export interface Sequence {
  id: string;
  project_id: string;
  name: string;
  description: string | null;
  status: string;
  spec_path: string | null;
  plan_path: string | null;
  wave_count: number | null;
  task_count: number | null;
  created_at: string;
  updated_at: string;
}

export interface Execution {
  id: string;
  sequence_id: string;
  continued_from: string | null;
  status: string;
  trigger: string;
  runtime: string;
  total_tasks: number;
  completed_tasks: number;
  current_wave: number;
  waves_state: string | null;
  config: string | null;
  source_branch: string | null;
  source_sha: string | null;
  work_branch: string | null;
  pr_url: string | null;
  git_sha_before: string | null;
  git_sha_after: string | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
}

export interface Event {
  id: string;
  execution_id: string;
  event_type: string;
  task_id: string | null;
  phase: string | null;
  payload: string;
  created_at: string;
}

export interface Command {
  id: string;
  execution_id: string;
  task_id: string;
  action: string | null;
  message: string | null;
  picked_up: boolean;
  created_at: string;
  resolved_at: string | null;
}

export interface ProjectRepository {
  id: string;
  project_id: string;
  path: string;
  label: string | null;
  created_at: string;
}

export interface ProjectContextFile {
  id: string;
  project_id: string;
  path: string;
  description: string | null;
  created_at: string;
}

export interface HealthResponse {
  status: string;
  version: string;
  active_executions: number;
}

export interface PlanGraphTask {
  id: string;
  title: string;
  agent: string;
  files: string[];
  depends: string[];
}

export interface PlanGraphFeature {
  name: string;
  files: string[];
  tasks: PlanGraphTask[];
}

export interface PlanGraphWave {
  index: number;
  name: string;
  description: string;
  foundation: PlanGraphTask[];
  features: PlanGraphFeature[];
  integration: PlanGraphTask[];
}

export interface PlanGraph {
  goal: string;
  waves: PlanGraphWave[];
}

// --- Projects ---

export const api = {
  // Health (unversioned)
  getHealth: () =>
    fetch(`${API_BASE}/health`).then((r) => r.json()) as Promise<HealthResponse>,

  // Projects
  listProjects: () => request<Project[]>("/projects"),
  getProject: (id: string) => request<Project>(`/projects/${id}`),
  createProject: (data: { name: string; description?: string }) =>
    request<Project>("/projects", { method: "POST", body: JSON.stringify(data) }),
  updateProject: (id: string, data: { name?: string; description?: string }) =>
    request<Project>(`/projects/${id}`, { method: "PATCH", body: JSON.stringify(data) }),
  deleteProject: (id: string) =>
    request<void>(`/projects/${id}`, { method: "DELETE" }),
  regenerateKey: (id: string) =>
    request<Project>(`/projects/${id}/regenerate-key`, { method: "POST" }),

  // Repositories
  listRepositories: (projectId: string) =>
    request<ProjectRepository[]>(`/projects/${projectId}/repositories`),
  addRepository: (projectId: string, data: { path: string; label?: string }) =>
    request<ProjectRepository>(`/projects/${projectId}/repositories`, {
      method: "POST",
      body: JSON.stringify(data),
    }),
  deleteRepository: (projectId: string, repoId: string) =>
    request<void>(`/projects/${projectId}/repositories/${repoId}`, { method: "DELETE" }),

  // Context Files
  listContextFiles: (projectId: string) =>
    request<ProjectContextFile[]>(`/projects/${projectId}/context-files`),
  addContextFile: (projectId: string, data: { path: string; description?: string }) =>
    request<ProjectContextFile>(`/projects/${projectId}/context-files`, {
      method: "POST",
      body: JSON.stringify(data),
    }),
  deleteContextFile: (projectId: string, fileId: string) =>
    request<void>(`/projects/${projectId}/context-files/${fileId}`, { method: "DELETE" }),

  // Sequences
  listSequences: (projectId: string) =>
    request<Sequence[]>(`/projects/${projectId}/sequences`),
  getSequence: (id: string) => request<Sequence>(`/sequences/${id}`),
  createSequence: (projectId: string, data: { name: string; description?: string }) =>
    request<Sequence>(`/projects/${projectId}/sequences`, {
      method: "POST",
      body: JSON.stringify(data),
    }),
  updateSequence: (id: string, data: { name?: string; description?: string }) =>
    request<Sequence>(`/sequences/${id}`, { method: "PATCH", body: JSON.stringify(data) }),
  deleteSequence: (id: string) =>
    request<void>(`/sequences/${id}`, { method: "DELETE" }),
  getSpec: (id: string) =>
    fetch(`${API_URL}/sequences/${id}/spec`).then((r) =>
      r.ok ? r.text() : null
    ),
  getPlan: (id: string) =>
    fetch(`${API_URL}/sequences/${id}/plan`).then((r) =>
      r.ok ? r.text() : null
    ),
  getPlanGraph: (id: string) =>
    fetch(`${API_URL}/sequences/${id}/plan-graph`).then((r) =>
      r.ok ? r.json() : null
    ) as Promise<PlanGraph | null>,

  // Executions
  listExecutions: (sequenceId: string) =>
    request<Execution[]>(`/sequences/${sequenceId}/executions`),
  getExecution: (id: string) => request<Execution>(`/executions/${id}`),
  createExecution: (sequenceId: string, data?: { runtime?: string; concurrency?: number }) =>
    request<Execution>(`/sequences/${sequenceId}/executions`, {
      method: "POST",
      body: JSON.stringify(data || {}),
    }),
  cancelExecution: (id: string) =>
    request<void>(`/executions/${id}/cancel`, { method: "POST" }),
  continueExecution: (id: string) =>
    request<Execution>(`/executions/${id}/continue`, { method: "POST" }),

  // Events
  listEvents: (executionId: string, since?: string) => {
    const params = new URLSearchParams();
    if (since) params.set("since", since);
    return request<Event[]>(`/executions/${executionId}/events?${params}`);
  },

  // Tasks
  listTasks: (executionId: string) =>
    request<Record<string, unknown>[]>(`/executions/${executionId}/tasks`),

  // Output
  getOutput: (executionId: string, taskId: string) =>
    fetch(`${API_URL}/executions/${executionId}/output/${taskId}`).then((r) =>
      r.ok ? r.text() : null
    ),

  // Transcript
  getTranscript: (executionId: string, taskId: string) =>
    fetch(`${API_URL}/executions/${executionId}/transcript/${taskId}`).then((r) =>
      r.ok ? r.text() : null
    ),

  // Task Log (human-readable)
  getTaskLog: (executionId: string, taskId: string) =>
    fetch(`${API_URL}/executions/${executionId}/task-logs/${taskId}`).then((r) =>
      r.ok ? r.text() : null
    ),

  listTaskLogs: (executionId: string) =>
    request<{ task_id: string; filename: string; agent: string }[]>(
      `/executions/${executionId}/task-logs`
    ),

  searchTaskLogs: (
    executionId: string,
    query: string,
    agent?: string,
  ) => {
    const params = new URLSearchParams({ q: query });
    if (agent) params.set("agent", agent);
    return request<{
      query: string;
      agent_filter: string | null;
      total_files: number;
      total_matches: number;
      results: {
        task_id: string;
        agent: string;
        filename: string;
        matches: { line_num: number; snippet: string }[];
        match_count: number;
      }[];
    }>(`/executions/${executionId}/task-logs/search?${params}`);
  },

  // Log
  getLog: (executionId: string) =>
    fetch(`${API_URL}/executions/${executionId}/log`).then((r) =>
      r.ok ? r.text() : null
    ),

  // Blockers
  listBlockers: (executionId: string) =>
    request<Command[]>(`/executions/${executionId}/blockers`),
  resolveBlocker: (executionId: string, commandId: string, data: { action: string }) =>
    request<Command>(`/executions/${executionId}/blockers/${commandId}`, {
      method: "POST",
      body: JSON.stringify(data),
    }),
};
