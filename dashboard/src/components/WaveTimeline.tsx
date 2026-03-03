"use client";

import Box from "@cloudscape-design/components/box";
import ProgressBar from "@cloudscape-design/components/progress-bar";
import SpaceBetween from "@cloudscape-design/components/space-between";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import type { Execution, Event } from "@/lib/api";

interface WaveTimelineProps {
  execution: Execution;
  events: Event[];
}

export default function WaveTimeline({ execution, events }: WaveTimelineProps) {
  // Parse waves_state if available
  const wavesState: { name: string; index: number; passed?: boolean }[] = [];
  if (execution.waves_state) {
    try {
      const parsed = JSON.parse(execution.waves_state);
      if (parsed.waves) wavesState.push(...parsed.waves);
    } catch {
      // ignore
    }
  }

  // Infer wave info from phase_changed events
  const phaseEvents = events.filter((e) => e.event_type === "phase_changed");
  const waveNames: string[] = [];
  for (const e of phaseEvents) {
    try {
      const p = JSON.parse(e.payload);
      if (p.wave_name && !waveNames.includes(p.wave_name)) {
        waveNames.push(p.wave_name);
      }
    } catch {
      // ignore
    }
  }

  const progress =
    execution.total_tasks > 0
      ? Math.round((execution.completed_tasks / execution.total_tasks) * 100)
      : 0;

  return (
    <SpaceBetween size="s">
      <ProgressBar
        value={progress}
        label={`Wave ${execution.current_wave + 1}`}
        description={`${execution.completed_tasks} of ${execution.total_tasks} tasks completed`}
        status={
          execution.status === "completed"
            ? "success"
            : execution.status === "failed"
            ? "error"
            : "in-progress"
        }
      />
      {waveNames.length > 0 && (
        <Box>
          <SpaceBetween direction="horizontal" size="xs">
            {waveNames.map((name, idx) => {
              const ws = wavesState.find((w) => w.name === name);
              const isCurrent = idx === execution.current_wave;
              const type = ws?.passed === true
                ? "success"
                : ws?.passed === false
                ? "error"
                : isCurrent
                ? "in-progress"
                : ("pending" as const);
              return (
                <StatusIndicator key={name} type={type}>
                  {name}
                </StatusIndicator>
              );
            })}
          </SpaceBetween>
        </Box>
      )}
    </SpaceBetween>
  );
}
