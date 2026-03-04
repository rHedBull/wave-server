"use client";

import { useCallback, useState } from "react";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Container from "@cloudscape-design/components/container";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import FormField from "@cloudscape-design/components/form-field";
import Header from "@cloudscape-design/components/header";
import Input from "@cloudscape-design/components/input";
import Select from "@cloudscape-design/components/select";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Spinner from "@cloudscape-design/components/spinner";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import { api } from "@/lib/api";

interface SearchResult {
  task_id: string;
  agent: string;
  filename: string;
  matches: { line_num: number; snippet: string }[];
  match_count: number;
}

interface SearchResponse {
  query: string;
  agent_filter: string | null;
  total_files: number;
  total_matches: number;
  results: SearchResult[];
}

interface TaskLogSearchProps {
  executionId: string;
  onSelectTask?: (taskId: string) => void;
}

const AGENT_OPTIONS = [
  { label: "All agents", value: "" },
  { label: "🔨 Worker", value: "worker" },
  { label: "🧪 Test Writer", value: "test-writer" },
  { label: "🔍 Verifier", value: "wave-verifier" },
];

function highlightMatch(snippet: string, query: string): React.ReactNode {
  if (!query) return snippet;
  const lowerSnippet = snippet.toLowerCase();
  const lowerQuery = query.toLowerCase();
  const parts: React.ReactNode[] = [];
  let lastIndex = 0;

  let idx = lowerSnippet.indexOf(lowerQuery, lastIndex);
  while (idx !== -1) {
    if (idx > lastIndex) {
      parts.push(snippet.slice(lastIndex, idx));
    }
    parts.push(
      <mark
        key={idx}
        style={{
          backgroundColor: "#fbbf24",
          color: "#000",
          padding: "0 1px",
          borderRadius: "2px",
        }}
      >
        {snippet.slice(idx, idx + query.length)}
      </mark>
    );
    lastIndex = idx + query.length;
    idx = lowerSnippet.indexOf(lowerQuery, lastIndex);
  }
  if (lastIndex < snippet.length) {
    parts.push(snippet.slice(lastIndex));
  }
  return parts;
}

function agentIcon(agent: string) {
  switch (agent) {
    case "worker":
      return "🔨";
    case "test-writer":
      return "🧪";
    case "wave-verifier":
      return "🔍";
    default:
      return "📋";
  }
}

export default function TaskLogSearch({
  executionId,
  onSelectTask,
}: TaskLogSearchProps) {
  const [query, setQuery] = useState("");
  const [agent, setAgent] = useState(AGENT_OPTIONS[0]);
  const [loading, setLoading] = useState(false);
  const [response, setResponse] = useState<SearchResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  const handleSearch = useCallback(async () => {
    const q = query.trim();
    if (!q) return;
    setLoading(true);
    setError(null);
    try {
      const res = await api.searchTaskLogs(
        executionId,
        q,
        agent.value || undefined
      );
      setResponse(res);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Search failed");
      setResponse(null);
    } finally {
      setLoading(false);
    }
  }, [executionId, query, agent]);

  const handleKeyDown = useCallback(
    (e: CustomEvent<{ keyCode: number; key: string }>) => {
      if (e.detail.key === "Enter") {
        handleSearch();
      }
    },
    [handleSearch]
  );

  return (
    <Container
      header={<Header variant="h3">🔎 Search Task Logs</Header>}
    >
      <SpaceBetween size="m">
        <div style={{ display: "flex", gap: "12px", alignItems: "flex-end" }}>
          <div style={{ flex: 1 }}>
            <FormField label="Search query">
              <Input
                value={query}
                onChange={({ detail }) => setQuery(detail.value)}
                onKeyDown={handleKeyDown as any}
                placeholder="Search across all task logs... (file path, error, function name)"
                type="search"
              />
            </FormField>
          </div>
          <div style={{ width: "180px" }}>
            <FormField label="Agent">
              <Select
                selectedOption={agent}
                onChange={({ detail }) =>
                  setAgent(detail.selectedOption as typeof agent)
                }
                options={AGENT_OPTIONS}
              />
            </FormField>
          </div>
          <Button
            variant="primary"
            onClick={handleSearch}
            loading={loading}
            disabled={!query.trim()}
          >
            Search
          </Button>
        </div>

        {error && (
          <StatusIndicator type="error">{error}</StatusIndicator>
        )}

        {loading && (
          <Box textAlign="center" padding="l">
            <Spinner />
          </Box>
        )}

        {response && !loading && (
          <SpaceBetween size="s">
            <Box color="text-status-inactive" fontSize="body-s">
              {response.total_matches === 0
                ? `No matches for "${response.query}"`
                : `${response.total_matches} match${response.total_matches !== 1 ? "es" : ""} across ${response.total_files} task log${response.total_files !== 1 ? "s" : ""}`}
              {response.agent_filter &&
                ` (filtered to ${response.agent_filter})`}
            </Box>

            {response.results.map((result) => (
              <ExpandableSection
                key={result.filename}
                variant="footer"
                headerText={
                  `${agentIcon(result.agent)} ${result.task_id} — ${result.match_count} match${result.match_count !== 1 ? "es" : ""}`
                }
                headerDescription={result.agent || undefined}
                defaultExpanded={response.results.length <= 5}
              >
                <SpaceBetween size="xxs">
                  {result.matches.map((m, i) => (
                    <div
                      key={i}
                      style={{
                        fontFamily: "monospace",
                        fontSize: "12px",
                        padding: "4px 8px",
                        backgroundColor: "var(--color-background-cell-hover, #f8f8f8)",
                        borderRadius: "4px",
                        cursor: "pointer",
                        display: "flex",
                        gap: "8px",
                      }}
                      onClick={() => onSelectTask?.(result.task_id)}
                      title="Click to open task log"
                    >
                      <span
                        style={{
                          color: "#888",
                          minWidth: "40px",
                          textAlign: "right",
                          userSelect: "none",
                        }}
                      >
                        L{m.line_num}
                      </span>
                      <span style={{ whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
                        {highlightMatch(m.snippet, response.query)}
                      </span>
                    </div>
                  ))}
                  <Box float="right">
                    <Button
                      variant="link"
                      onClick={() => onSelectTask?.(result.task_id)}
                    >
                      Open full log →
                    </Button>
                  </Box>
                </SpaceBetween>
              </ExpandableSection>
            ))}
          </SpaceBetween>
        )}
      </SpaceBetween>
    </Container>
  );
}
