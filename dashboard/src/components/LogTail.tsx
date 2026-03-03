"use client";

import { useEffect, useRef, useState } from "react";
import Box from "@cloudscape-design/components/box";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import { api } from "@/lib/api";

interface LogTailProps {
  executionId: string;
  isActive: boolean;
}

export default function LogTail({ executionId, isActive }: LogTailProps) {
  const [log, setLog] = useState<string>("");
  const containerRef = useRef<HTMLPreElement>(null);

  useEffect(() => {
    const fetchLog = () => {
      api.getLog(executionId).then((content) => {
        if (content) setLog(content);
      });
    };

    fetchLog();
    if (!isActive) return;

    const timer = setInterval(fetchLog, 3000);
    return () => clearInterval(timer);
  }, [executionId, isActive]);

  useEffect(() => {
    if (containerRef.current) {
      containerRef.current.scrollTop = containerRef.current.scrollHeight;
    }
  }, [log]);

  return (
    <Container header={<Header variant="h3">Log</Header>}>
      <Box variant="code">
        <pre
          ref={containerRef}
          style={{
            whiteSpace: "pre-wrap",
            margin: 0,
            fontSize: "12px",
            maxHeight: "300px",
            overflow: "auto",
          }}
        >
          {log || "No log output yet"}
        </pre>
      </Box>
    </Container>
  );
}
