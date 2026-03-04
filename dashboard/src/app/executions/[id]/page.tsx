"use client";

import { use, useState } from "react";
import { useRouter } from "next/navigation";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import ColumnLayout from "@cloudscape-design/components/column-layout";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Spinner from "@cloudscape-design/components/spinner";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import AppShell from "@/components/AppShell";
import BlockerBanner from "@/components/BlockerBanner";
import LogTail from "@/components/LogTail";
import TaskDetail from "@/components/TaskDetail";
import TaskLogSearch from "@/components/TaskLogSearch";
import TaskTable from "@/components/TaskTable";
import WaveTimeline from "@/components/WaveTimeline";
import { useExecution } from "@/hooks/useExecution";
import { api } from "@/lib/api";

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

  return (
    <AppShell
      breadcrumbs={[
        { text: "Projects", href: "/projects" },
        { text: "Execution", href: `/executions/${id}` },
      ]}
    >
      <SpaceBetween size="l">
        <Header
          variant="h1"
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
            <Box>{execution.runtime}</Box>
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

        {/* Blocker Banner */}
        <BlockerBanner executionId={id} isActive={isActive} />

        {/* Wave Timeline */}
        <Container header={<Header variant="h3">Wave Progress</Header>}>
          <WaveTimeline execution={execution} events={events} />
        </Container>

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

        {/* Task Detail */}
        {selectedTask && (
          <TaskDetail
            executionId={id}
            taskId={selectedTask}
            task={tasks.find((t: Record<string, unknown>) => t.task_id === selectedTask)}
          />
        )}

        {/* Log Tail */}
        <LogTail executionId={id} isActive={isActive} />
      </SpaceBetween>
    </AppShell>
  );
}
