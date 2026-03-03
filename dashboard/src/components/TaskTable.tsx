"use client";

import { useState } from "react";
import Badge from "@cloudscape-design/components/badge";
import Box from "@cloudscape-design/components/box";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Table from "@cloudscape-design/components/table";
import Header from "@cloudscape-design/components/header";

interface TaskSummary {
  task_id: string;
  status: string;
  phase?: string;
  title?: string;
  agent?: string;
  exit_code?: number;
  duration_ms?: number;
}

interface TaskTableProps {
  tasks: Record<string, unknown>[];
  executionId: string;
  onSelectTask?: (taskId: string) => void;
}

function statusType(status: string) {
  switch (status) {
    case "completed":
      return "success" as const;
    case "failed":
      return "error" as const;
    case "running":
      return "in-progress" as const;
    case "skipped":
      return "stopped" as const;
    default:
      return "pending" as const;
  }
}

function agentColor(agent: string) {
  switch (agent) {
    case "test-writer":
      return "blue";
    case "wave-verifier":
      return "green";
    default:
      return "grey";
  }
}

export default function TaskTable({
  tasks,
  executionId,
  onSelectTask,
}: TaskTableProps) {
  const items = tasks as unknown as TaskSummary[];

  return (
    <Table
      header={<Header variant="h3">Tasks</Header>}
      items={items}
      columnDefinitions={[
        {
          id: "status",
          header: "Status",
          cell: (item) => (
            <StatusIndicator type={statusType(item.status)}>
              {item.status}
            </StatusIndicator>
          ),
          width: 120,
        },
        {
          id: "task_id",
          header: "Task ID",
          cell: (item) => (
            <span
              style={{ fontWeight: "bold", cursor: "pointer", color: "#0972d3" }}
              onClick={() => onSelectTask?.(item.task_id)}
            >
              {item.task_id}
            </span>
          ),
          width: 180,
        },
        {
          id: "title",
          header: "Title",
          cell: (item) => item.title || "—",
        },
        {
          id: "agent",
          header: "Agent",
          cell: (item) =>
            item.agent ? (
              <Badge color={agentColor(item.agent)}>{item.agent}</Badge>
            ) : (
              "—"
            ),
          width: 130,
        },
        {
          id: "phase",
          header: "Phase",
          cell: (item) => item.phase || "—",
          width: 120,
        },
        {
          id: "duration",
          header: "Duration",
          cell: (item) =>
            item.duration_ms
              ? `${(item.duration_ms / 1000).toFixed(1)}s`
              : "—",
          width: 100,
        },
      ]}
      empty={
        <Box textAlign="center" color="inherit" padding="l">
          No tasks yet
        </Box>
      }
    />
  );
}
