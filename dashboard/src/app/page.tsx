"use client";

import { useCallback } from "react";
import { useRouter } from "next/navigation";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Cards from "@cloudscape-design/components/cards";
import ColumnLayout from "@cloudscape-design/components/column-layout";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import SpaceBetween from "@cloudscape-design/components/space-between";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import AppShell from "@/components/AppShell";
import { usePolling } from "@/hooks/usePolling";
import { api, type Project, type HealthResponse } from "@/lib/api";

export default function HomePage() {
  const router = useRouter();
  const healthFetcher = useCallback(() => api.getHealth(), []);
  const projectsFetcher = useCallback(() => api.listProjects(), []);
  const { data: health } = usePolling(healthFetcher, 10000);
  const { data: projects, loading: projectsLoading } = usePolling(
    projectsFetcher,
    10000
  );

  return (
    <AppShell activeHref="/">
      <SpaceBetween size="l">
        <Header variant="h1">Dashboard</Header>

        {/* Health Overview */}
        <ColumnLayout columns={3}>
          <Container>
            <Box variant="awsui-key-label">Status</Box>
            <Box>
              {health ? (
                <StatusIndicator type="success">Healthy</StatusIndicator>
              ) : (
                <StatusIndicator type="loading">Checking…</StatusIndicator>
              )}
            </Box>
          </Container>
          <Container>
            <Box variant="awsui-key-label">Version</Box>
            <Box>{health?.version ?? "—"}</Box>
          </Container>
          <Container>
            <Box variant="awsui-key-label">Active Executions</Box>
            <Box>{health?.active_executions ?? "—"}</Box>
          </Container>
        </ColumnLayout>

        {/* Projects */}
        <Cards
          header={
            <Header
              variant="h2"
              actions={
                <Button
                  variant="primary"
                  onClick={() => router.push("/projects")}
                >
                  View all projects
                </Button>
              }
            >
              Projects
            </Header>
          }
          loading={projectsLoading}
          loadingText="Loading projects"
          items={projects || []}
          cardDefinition={{
            header: (item) => (
              <Box fontWeight="bold">
                <a
                  href={`/projects/${item.id}`}
                  onClick={(e) => {
                    e.preventDefault();
                    router.push(`/projects/${item.id}`);
                  }}
                  style={{ textDecoration: "none", color: "inherit" }}
                >
                  {item.name}
                </a>
              </Box>
            ),
            sections: [
              {
                id: "description",
                header: "Description",
                content: (item) => item.description || "—",
              },
              {
                id: "created",
                header: "Created",
                content: (item) =>
                  new Date(item.created_at).toLocaleDateString(),
              },
            ],
          }}
          empty={
            <Box textAlign="center" color="inherit" padding="l">
              <b>No projects</b>
              <Box padding={{ bottom: "s" }} variant="p" color="inherit">
                Create a project to get started.
              </Box>
              <Button onClick={() => router.push("/projects")}>
                Go to Projects
              </Button>
            </Box>
          }
        />
      </SpaceBetween>
    </AppShell>
  );
}
