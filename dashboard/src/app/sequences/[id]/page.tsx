"use client";

import { use, useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Container from "@cloudscape-design/components/container";
import FormField from "@cloudscape-design/components/form-field";
import Header from "@cloudscape-design/components/header";
import Input from "@cloudscape-design/components/input";
import SpaceBetween from "@cloudscape-design/components/space-between";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Table from "@cloudscape-design/components/table";
import Tabs from "@cloudscape-design/components/tabs";
import Textarea from "@cloudscape-design/components/textarea";
import Flashbar from "@cloudscape-design/components/flashbar";
import AppShell from "@/components/AppShell";
import ConfirmDeleteModal from "@/components/ConfirmDeleteModal";
import MarkdownView from "@/components/MarkdownView";
import PlanGraphView from "@/components/PlanGraph";
import { usePolling } from "@/hooks/usePolling";
import { api, type Execution, type PlanGraph, type Project, type Sequence } from "@/lib/api";

function statusType(status: string) {
  switch (status) {
    case "completed":
      return "success" as const;
    case "failed":
      return "error" as const;
    case "running":
    case "executing":
      return "in-progress" as const;
    case "cancelled":
      return "stopped" as const;
    default:
      return "pending" as const;
  }
}

export default function SequenceDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const router = useRouter();

  const seqFetcher = useCallback(() => api.getSequence(id), [id]);
  const execFetcher = useCallback(() => api.listExecutions(id), [id]);

  const { data: sequence, refetch: refetchSeq } = usePolling(seqFetcher, 10000);
  const { data: executions, loading: execLoading, refetch: refetchExecs } = usePolling(execFetcher, 5000);
  const [project, setProject] = useState<Project | null>(null);

  const [spec, setSpec] = useState<string | null>(null);
  const [plan, setPlan] = useState<string | null>(null);
  const [planGraph, setPlanGraph] = useState<PlanGraph | null>(null);
  const [planGraphLoading, setPlanGraphLoading] = useState(true);

  const [editName, setEditName] = useState("");
  const [editDesc, setEditDesc] = useState("");
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);
  const [initialized, setInitialized] = useState(false);
  const [activeTab, setActiveTab] = useState<string | null>(null);
  const [runError, setRunError] = useState<string | null>(null);

  useEffect(() => {
    api.getSpec(id).then(setSpec);
    api.getPlan(id).then(setPlan);
    setPlanGraphLoading(true);
    api.getPlanGraph(id).then(setPlanGraph).finally(() => setPlanGraphLoading(false));
  }, [id]);

  useEffect(() => {
    if (sequence?.project_id) {
      api.getProject(sequence.project_id).then(setProject).catch(() => {});
    }
  }, [sequence?.project_id]);

  if (sequence && !initialized) {
    setEditName(sequence.name);
    setEditDesc(sequence.description || "");
    setInitialized(true);
  }

  // Default to "executions" tab if any executions exist (wait for data to load)
  if (activeTab === null && !execLoading && executions !== null) {
    setActiveTab(executions.length > 0 ? "executions" : "spec");
  }



  const handleSave = async () => {
    setSaving(true);
    try {
      await api.updateSequence(id, {
        name: editName,
        description: editDesc,
      });
      setEditing(false);
      refetchSeq();
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async () => {
    const projectId = sequence?.project_id;
    await api.deleteSequence(id);
    router.push(projectId ? `/projects/${projectId}` : "/projects");
  };

  const handleCreateExecution = async () => {
    setRunError(null);
    try {
      const exec = await api.createExecution(id);
      refetchExecs();
      router.push(`/executions/${exec.id}`);
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      // Strip the leading HTTP status code if present (e.g. "422: ...")
      setRunError(message.replace(/^\d+:\s*/, "").replace(/^.*?"detail"\s*:\s*"([^"]+)".*$/, "$1"));
    }
  };

  if (!sequence) return null;

  return (
    <AppShell
      breadcrumbs={[
        { text: "Projects", href: "/projects" },
        {
          text: project ? project.name : "…",
          href: project ? `/projects/${project.id}` : "/projects",
        },
        { text: sequence.name, href: `/sequences/${id}` },
      ]}
    >
      <SpaceBetween size="l">
        <Header
          variant="h1"
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              {!editing && (
                <Button onClick={() => setEditing(true)}>Edit</Button>
              )}
              <Button variant="primary" onClick={handleCreateExecution}>
                Run execution
              </Button>
            </SpaceBetween>
          }
        >
          {sequence.name}
          <Box margin={{ left: "s" }} display="inline-block">
            <StatusIndicator type={statusType(sequence.status)}>
              {sequence.status}
            </StatusIndicator>
          </Box>
        </Header>

        {runError && (
          <Flashbar
            items={[
              {
                type: "error",
                dismissible: true,
                onDismiss: () => setRunError(null),
                header: "Cannot start execution",
                content: runError,
                id: "run-error",
              },
            ]}
          />
        )}

        {editing && (
          <Container header={<Header variant="h2">Edit Sequence</Header>}>
            <SpaceBetween size="l">
              <FormField label="Name">
                <Input
                  value={editName}
                  onChange={({ detail }) => setEditName(detail.value)}
                />
              </FormField>
              <FormField label="Description">
                <Textarea
                  value={editDesc}
                  onChange={({ detail }) => setEditDesc(detail.value)}
                  rows={3}
                />
              </FormField>
              <SpaceBetween direction="horizontal" size="xs">
                <Button variant="primary" onClick={handleSave} loading={saving}>
                  Save
                </Button>
                <Button variant="link" onClick={() => setEditing(false)}>
                  Cancel
                </Button>
              </SpaceBetween>
            </SpaceBetween>
          </Container>
        )}

        <Tabs
          activeTabId={activeTab || "spec"}
          onChange={({ detail }) => setActiveTab(detail.activeTabId)}
          tabs={[
            {
              label: "Spec",
              id: "spec",
              content: <MarkdownView title="Spec" content={spec} />,
            },
            {
              label: "Plan",
              id: "plan",
              content: <MarkdownView title="Plan" content={plan} />,
            },
            {
              label: "Graph",
              id: "graph",
              content: (
                <PlanGraphView
                  graph={planGraph}
                  loading={planGraphLoading}
                />
              ),
            },
            {
              label: "Executions",
              id: "executions",
              content: (
                <Table
                  header={<Header variant="h3">Executions</Header>}
                  loading={execLoading}
                  loadingText="Loading executions"
                  items={executions || []}
                  columnDefinitions={[
                    {
                      id: "id",
                      header: "Execution",
                      cell: (item) => (
                        <Button
                          variant="inline-link"
                          onClick={() =>
                            router.push(`/executions/${item.id}`)
                          }
                        >
                          {item.id.slice(0, 8)}…
                        </Button>
                      ),
                      width: 120,
                    },
                    {
                      id: "status",
                      header: "Status",
                      cell: (item) => (
                        <StatusIndicator type={statusType(item.status)}>
                          {item.status}
                        </StatusIndicator>
                      ),
                      width: 130,
                    },
                    {
                      id: "trigger",
                      header: "Trigger",
                      cell: (item) => item.trigger,
                      width: 100,
                    },
                    {
                      id: "branch",
                      header: "Branch",
                      cell: (item) => (
                        <SpaceBetween direction="horizontal" size="xxs">
                          {item.work_branch ? (
                            <code style={{ fontSize: "0.85em" }}>{item.work_branch}</code>
                          ) : (
                            "—"
                          )}
                          {item.pr_url && (
                            <a
                              href={item.pr_url}
                              target="_blank"
                              rel="noopener noreferrer"
                              title="Open PR"
                              onClick={(e) => e.stopPropagation()}
                            >
                              PR
                            </a>
                          )}
                        </SpaceBetween>
                      ),
                      width: 200,
                    },
                    {
                      id: "progress",
                      header: "Progress",
                      cell: (item) =>
                        `${item.completed_tasks}/${item.total_tasks} tasks`,
                      width: 120,
                    },
                    {
                      id: "started",
                      header: "Started",
                      cell: (item) =>
                        item.started_at
                          ? new Date(item.started_at).toLocaleString()
                          : "—",
                    },
                    {
                      id: "finished",
                      header: "Finished",
                      cell: (item) =>
                        item.finished_at
                          ? new Date(item.finished_at).toLocaleString()
                          : "—",
                    },
                    {
                      id: "duration",
                      header: "Duration",
                      cell: (item) => {
                        if (!item.started_at) return "—";
                        const start = new Date(item.started_at).getTime();
                        const end = item.finished_at
                          ? new Date(item.finished_at).getTime()
                          : Date.now();
                        const secs = Math.floor((end - start) / 1000);
                        if (secs < 60) return `${secs}s`;
                        const mins = Math.floor(secs / 60);
                        const rem = secs % 60;
                        if (mins < 60) return `${mins}m ${rem}s`;
                        const hrs = Math.floor(mins / 60);
                        return `${hrs}h ${mins % 60}m`;
                      },
                      width: 100,
                    },
                  ]}
                  empty={
                    <Box textAlign="center" color="inherit" padding="l">
                      <b>No executions</b>
                    </Box>
                  }
                />
              ),
            },
          ]}
        />

        <Container header={<Header variant="h2">Danger zone</Header>}>
          <span className="btn-danger">
            <Button variant="primary" onClick={() => setShowDeleteConfirm(true)}>
              Delete sequence
            </Button>
          </span>
        </Container>
      </SpaceBetween>

      <ConfirmDeleteModal
        visible={showDeleteConfirm}
        onDismiss={() => setShowDeleteConfirm(false)}
        onConfirm={handleDelete}
        resourceName={sequence.name}
        resourceType="sequence"
      />
    </AppShell>
  );
}
