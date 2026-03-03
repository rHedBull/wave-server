"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, type Event, type Execution } from "@/lib/api";

export function useExecution(executionId: string) {
  const [execution, setExecution] = useState<Execution | null>(null);
  const [events, setEvents] = useState<Event[]>([]);
  const [tasks, setTasks] = useState<Record<string, unknown>[]>([]);
  const [loading, setLoading] = useState(true);
  const lastEventTime = useRef<string | undefined>(undefined);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const isActive = execution?.status === "queued" || execution?.status === "running";

  const fetchExecution = useCallback(async () => {
    try {
      const exec = await api.getExecution(executionId);
      setExecution(exec);
    } catch {
      // ignore
    }
  }, [executionId]);

  const fetchEvents = useCallback(async () => {
    try {
      const newEvents = await api.listEvents(executionId, lastEventTime.current);
      if (newEvents.length > 0) {
        setEvents((prev) => [...prev, ...newEvents]);
        lastEventTime.current = newEvents[newEvents.length - 1].created_at;
      }
    } catch {
      // ignore
    }
  }, [executionId]);

  const fetchTasks = useCallback(async () => {
    try {
      const t = await api.listTasks(executionId);
      setTasks(t);
    } catch {
      // ignore
    }
  }, [executionId]);

  // Initial load
  useEffect(() => {
    Promise.all([fetchExecution(), fetchEvents(), fetchTasks()]).then(() =>
      setLoading(false)
    );
  }, [fetchExecution, fetchEvents, fetchTasks]);

  // Polling
  useEffect(() => {
    if (!isActive) return;

    const poll = () => {
      fetchExecution();
      fetchEvents();
    };

    timerRef.current = setInterval(poll, 2000);
    // Tasks poll less frequently
    const taskTimer = setInterval(fetchTasks, 5000);

    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
      clearInterval(taskTimer);
    };
  }, [isActive, fetchExecution, fetchEvents, fetchTasks]);

  return { execution, events, tasks, loading, isActive, refetch: fetchExecution };
}
