"use client";

import { useEffect, useState } from "react";
import Box from "@cloudscape-design/components/box";
import Container from "@cloudscape-design/components/container";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import Header from "@cloudscape-design/components/header";
import Spinner from "@cloudscape-design/components/spinner";
import { api } from "@/lib/api";

interface TaskDetailProps {
  executionId: string;
  taskId: string;
  onClose?: () => void;
}

export default function TaskDetail({
  executionId,
  taskId,
  onClose,
}: TaskDetailProps) {
  const [output, setOutput] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    api.getOutput(executionId, taskId).then((content) => {
      setOutput(content);
      setLoading(false);
    });
  }, [executionId, taskId]);

  return (
    <Container
      header={
        <Header variant="h3" description={`Task output for ${taskId}`}>
          Task: {taskId}
        </Header>
      }
    >
      {loading ? (
        <Spinner />
      ) : output ? (
        <Box variant="code">
          <pre
            style={{
              whiteSpace: "pre-wrap",
              margin: 0,
              fontSize: "12px",
              maxHeight: "400px",
              overflow: "auto",
            }}
          >
            {output}
          </pre>
        </Box>
      ) : (
        <Box color="text-status-inactive">No output available</Box>
      )}
    </Container>
  );
}
