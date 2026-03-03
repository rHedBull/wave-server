"use client";

import { useEffect, useState } from "react";
import Box from "@cloudscape-design/components/box";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import Spinner from "@cloudscape-design/components/spinner";
import Tabs from "@cloudscape-design/components/tabs";
import { api } from "@/lib/api";

interface TaskDetailProps {
  executionId: string;
  taskId: string;
  task?: Record<string, unknown>;
  onClose?: () => void;
}

export default function TaskDetail({
  executionId,
  taskId,
  task,
  onClose,
}: TaskDetailProps) {
  const [data, setData] = useState<{ output: string | null; transcript: string | null } | null>(null);
  const [activeTab, setActiveTab] = useState("output");

  const loading = data === null;
  const output = data?.output ?? null;
  const transcript = data?.transcript ?? null;

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      const [out, trans] = await Promise.all([
        api.getOutput(executionId, taskId),
        api.getTranscript(executionId, taskId),
      ]);
      if (!cancelled) {
        setData({ output: out, transcript: trans });
      }
    };
    load();
    return () => { cancelled = true; };
  }, [executionId, taskId]);

  const metadata = task
    ? [
        task.agent && `Agent: ${task.agent}`,
        task.phase && `Phase: ${task.phase}`,
        task.duration_ms != null && `Duration: ${task.duration_ms}ms`,
        task.exit_code != null && `Exit code: ${task.exit_code}`,
      ].filter(Boolean)
    : [];

  const preStyle = {
    whiteSpace: "pre-wrap" as const,
    margin: 0,
    fontSize: "12px",
    maxHeight: "400px",
    overflow: "auto" as const,
  };

  return (
    <Container
      header={
        <Header
          variant="h3"
          description={
            metadata.length > 0
              ? metadata.join(" | ")
              : `Task output for ${taskId}`
          }
        >
          Task: {taskId}
        </Header>
      }
    >
      {loading ? (
        <Spinner />
      ) : (
        <Tabs
          activeTabId={activeTab}
          onChange={({ detail }) => setActiveTab(detail.activeTabId)}
          tabs={[
            {
              id: "output",
              label: "Output",
              content: output ? (
                <Box variant="code">
                  <pre style={preStyle}>{output}</pre>
                </Box>
              ) : (
                <Box color="text-status-inactive">No output available</Box>
              ),
            },
            {
              id: "transcript",
              label: "Transcript",
              disabled: !transcript,
              content: transcript ? (
                <Box variant="code">
                  <pre style={preStyle}>{transcript}</pre>
                </Box>
              ) : (
                <Box color="text-status-inactive">No transcript available</Box>
              ),
            },
          ]}
        />
      )}
    </Container>
  );
}
