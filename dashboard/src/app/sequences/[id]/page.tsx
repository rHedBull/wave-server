"use client";

import { use, useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Header from "@cloudscape-design/components/header";
import SpaceBetween from "@cloudscape-design/components/space-between";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Table from "@cloudscape-design/components/table";
import Tabs from "@cloudscape-design/components/tabs";
import AppShell from "@/components/AppShell";
import MarkdownView from "@/components/MarkdownView";
import { usePolling } from "@/hooks/usePolling";
import { api, type Execution, type Sequence } from "@/lib/api";

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

  const { data: sequence } = usePolling(seqFetcher, 10000);
  const { data: executions, loading: execLoading, refetch: refetchExecs } = usePolling(execFetcher, 5000);

  const [spec, setSpec] = useState<string | null>(null);
  const [plan, setPlan] = useState<string | null>(null);

  useEffect(() => {
    api.getSpec(id).then(setSpec);
    api.getPlan(id).then(setPlan);
  }, [id]);

  const handleCreateExecution = async () => {
    const exec = await api.createExecution(id);
    refetchExecs();
    router.push(`/executions/${exec.id}`);
  };

  if (!sequence) return null;

  return (
    <AppShell
      breadcrumbs={[
        { text: "Projects", href: "/projects" },
        { text: "Sequence", href: `/sequences/${id}` },
      ]}
    >
      <SpaceBetween size="l">
        <Header
          variant="h1"
          actions={
            <Button variant="primary" onClick={handleCreateExecution}>
              Run execution
            </Button>
          }
        >
          {sequence.name}
          <Box margin={{ left: "s" }} display="inline-block">
            <StatusIndicator type={statusType(sequence.status)}>
              {sequence.status}
            </StatusIndicator>
          </Box>
        </Header>

        <Tabs
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
                      id: "progress",
                      header: "Progress",
                      cell: (item) =>
                        `${item.completed_tasks}/${item.total_tasks} tasks`,
                      width: 120,
                    },
                    {
                      id: "runtime",
                      header: "Runtime",
                      cell: (item) => item.runtime,
                      width: 100,
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
                      id: "actions",
                      header: "Actions",
                      cell: (item) => (
                        <Button
                          variant="inline-link"
                          onClick={() =>
                            router.push(`/executions/${item.id}`)
                          }
                        >
                          View
                        </Button>
                      ),
                      width: 80,
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
      </SpaceBetween>
    </AppShell>
  );
}
