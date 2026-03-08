"use client";

import { use, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import ColumnLayout from "@cloudscape-design/components/column-layout";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Spinner from "@cloudscape-design/components/spinner";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Badge from "@cloudscape-design/components/badge";
import SplitPanel from "@cloudscape-design/components/split-panel";
import AppShell from "@/components/AppShell";
import BlockerBanner from "@/components/BlockerBanner";
import CopyableId from "@/components/CopyableId";
import LogTail from "@/components/LogTail";
import PlanGraphView, { type TaskStatusMap } from "@/components/PlanGraph";
import TaskDetail from "@/components/TaskDetail";
import TaskLogSearch from "@/components/TaskLogSearch";
import TaskTable from "@/components/TaskTable";
import WaveTimeline from "@/components/WaveTimeline";
import { useExecution } from "@/hooks/useExecution";
import { api, type PlanGraph, type Project, type Sequence } from "@/lib/api";

function statusType(status: string) {
  switch (status) {
    case "completed":
      return "success" as const;
    case "failed":
      return "error" as const;
    case "running":
      return "in-progress" as const;
    case "cancelled":
      return "stopped" as const;
    default:
      return "pending" as const;
  }
}

export default function ExecutionPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const router = useRouter();
  const { execution, events, tasks, loading, isActive, refetch } =
    useExecution(id);
  const [selectedTask, setSelectedTask] = useState<string | null>(null);
  const [sequence, setSequence] = useState<Sequence | null>(null);
  const [project, setProject] = useState<Project | null>(null);
  const [planGraph, setPlanGraph] = useState<PlanGraph | null>(null);

  useEffect(() => {
    if (execution?.sequence_id) {
      api.getSequence(execution.sequence_id).then((seq) => {
        setSequence(seq);
        if (seq.project_id) {
          api.getProject(seq.project_id).then(setProject).catch(() => {});
        }
      }).catch(() => {});
      api.getPlanGraph(execution.sequence_id).then(setPlanGraph).catch(() => {});
    }
  }, [execution?.sequence_id]);

  // Build task status map from execution tasks
  const taskStatuses: TaskStatusMap = useMemo(() => {
    const map: TaskStatusMap = {};
    for (const t of tasks) {
      const tid = t.task_id as string;
      const status = t.status as string;
      if (tid && status) map[tid] = status;
    }
    return map;
  }, [tasks]);

  const handleCancel = async () => {
    await api.cancelExecution(id);
    refetch();
  };

  const handleContinue = async () => {
    const newExec = await api.continueExecution(id);
    router.push(`/executions/${newExec.id}`);
  };

  if (loading) {
    return (
      <AppShell>
        <Box textAlign="center" padding="xxl">
          <Spinner size="large" />
        </Box>
      </AppShell>
    );
  }

  if (!execution) {
    return (
      <AppShell>
        <Box textAlign="center" padding="xxl">
          Execution not found
        </Box>
      </AppShell>
    );
  }

  const selectedTaskData = selectedTask
    ? tasks.find((t: Record<string, unknown>) => t.task_id === selectedTask)
    : undefined;

  return (
    <AppShell
      breadcrumbs={[
        { text: "Projects", href: "/projects" },
        {
          text: project ? project.name : "…",
          href: project ? `/projects/${project.id}` : "/projects",
        },
        {
          text: sequence ? sequence.name : "…",
          href: sequence ? `/sequences/${sequence.id}` : "#",
        },
        { text: "Execution", href: `/executions/${id}` },
      ]}
      splitPanel={
        selectedTask ? (
          <SplitPanel
            header={`Task: ${selectedTask}`}
            closeBehavior="hide"
          >
            <TaskDetail
              executionId={id}
              taskId={selectedTask}
              task={selectedTaskData}
            />
          </SplitPanel>
        ) : undefined
      }
      splitPanelOpen={!!selectedTask}
      onSplitPanelToggle={(open) => {
        if (!open) setSelectedTask(null);
      }}
    >
      <SpaceBetween size="l">
        <Header
          variant="h1"
          description={<CopyableId id={id} label="Execution" />}
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              {isActive && (
                <Button onClick={handleCancel}>Cancel</Button>
              )}
              {(execution.status === "failed" ||
                execution.status === "cancelled") && (
                <Button variant="primary" onClick={handleContinue}>
                  Continue
                </Button>
              )}
            </SpaceBetween>
          }
        >
          Execution
          <Box margin={{ left: "s" }} display="inline-block">
            <StatusIndicator type={statusType(execution.status)}>
              {execution.status}
            </StatusIndicator>
          </Box>
        </Header>

        {/* Summary */}
        <ColumnLayout columns={4}>
          <Container>
            <Box variant="awsui-key-label">Runtime</Box>
            <Box>
              <Badge color={execution.runtime === "claude" ? "blue" : "grey"}>
                {execution.runtime}
              </Badge>
            </Box>
          </Container>
          <Container>
            <Box variant="awsui-key-label">Trigger</Box>
            <Box>{execution.trigger}</Box>
          </Container>
          <Container>
            <Box variant="awsui-key-label">Progress</Box>
            <Box>
              {execution.completed_tasks}/{execution.total_tasks} tasks
            </Box>
          </Container>
          <Container>
            <Box variant="awsui-key-label">Started</Box>
            <Box>
              {execution.started_at
                ? new Date(execution.started_at).toLocaleString()
                : "—"}
            </Box>
          </Container>
        </ColumnLayout>

        {/* Git / Branch Info */}
        {(execution.work_branch || execution.source_branch) && (
          <Container header={<Header variant="h3">Git</Header>}>
            <ColumnLayout columns={3}>
              <div>
                <Box variant="awsui-key-label">Source</Box>
                <Box>
                  <code>{execution.source_branch || "—"}</code>
                  {execution.source_sha && (
                    <Box variant="small" color="text-body-secondary">
                      {execution.source_sha.slice(0, 8)}
                    </Box>
                  )}
                </Box>
              </div>
              <div>
                <Box variant="awsui-key-label">Work branch</Box>
                <Box>
                  <code>{execution.work_branch || "—"}</code>
                </Box>
              </div>
              <div>
                <Box variant="awsui-key-label">Pull Request</Box>
                <Box>
                  {execution.pr_url ? (
                    <a
                      href={execution.pr_url}
                      target="_blank"
                      rel="noopener noreferrer"
                    >
                      {execution.pr_url.replace(/.*\/pull\//, "#")}
                    </a>
                  ) : execution.status === "completed" ? (
                    "No PR created"
                  ) : execution.status === "running" ? (
                    "Pending…"
                  ) : (
                    "—"
                  )}
                </Box>
              </div>
            </ColumnLayout>
          </Container>
        )}

        {/* Blocker Banner */}
        <BlockerBanner executionId={id} isActive={isActive} />

        {/* Wave Timeline */}
        <Container header={<Header variant="h3">Wave Progress</Header>}>
          <WaveTimeline execution={execution} events={events} />
        </Container>

        {/* Execution Graph */}
        {planGraph && (
          <PlanGraphView
            graph={planGraph}
            taskStatuses={taskStatuses}
            onSelectTask={setSelectedTask}
          />
        )}

        {/* Task Table */}
        <TaskTable
          tasks={tasks}
          onSelectTask={setSelectedTask}
        />

        {/* Task Log Search */}
        <TaskLogSearch
          executionId={id}
          onSelectTask={setSelectedTask}
        />

        {/* Log Tail */}
        <LogTail executionId={id} isActive={isActive} />
      </SpaceBetween>
    </AppShell>
  );
}
