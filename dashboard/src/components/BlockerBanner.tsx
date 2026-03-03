"use client";

import { useCallback, useState } from "react";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Flashbar, {
  type FlashbarProps,
} from "@cloudscape-design/components/flashbar";
import SpaceBetween from "@cloudscape-design/components/space-between";
import { usePolling } from "@/hooks/usePolling";
import { api, type Command } from "@/lib/api";

interface BlockerBannerProps {
  executionId: string;
  isActive: boolean;
}

export default function BlockerBanner({
  executionId,
  isActive,
}: BlockerBannerProps) {
  const fetcher = useCallback(
    () => api.listBlockers(executionId),
    [executionId]
  );
  const { data: blockers, refetch } = usePolling(fetcher, 3000, isActive);
  const [resolving, setResolving] = useState<string | null>(null);

  const handleResolve = async (commandId: string, action: "retry" | "skip") => {
    setResolving(commandId);
    try {
      await api.resolveBlocker(executionId, commandId, { action });
      refetch();
    } finally {
      setResolving(null);
    }
  };

  if (!blockers || blockers.length === 0) return null;

  const items: FlashbarProps.MessageDefinition[] = blockers.map((blocker) => ({
    type: "warning" as const,
    dismissible: false,
    header: `Blocker: Task ${blocker.task_id}`,
    content: (
      <SpaceBetween size="s">
        {blocker.message && <Box>{blocker.message}</Box>}
        <SpaceBetween direction="horizontal" size="xs">
          <Button
            variant="primary"
            loading={resolving === blocker.id}
            onClick={() => handleResolve(blocker.id, "retry")}
          >
            Retry
          </Button>
          <Button
            loading={resolving === blocker.id}
            onClick={() => handleResolve(blocker.id, "skip")}
          >
            Skip
          </Button>
        </SpaceBetween>
      </SpaceBetween>
    ),
    id: blocker.id,
  }));

  return <Flashbar items={items} />;
}
