"use client";

import { useCallback, useState } from "react";
import { useRouter } from "next/navigation";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Cards from "@cloudscape-design/components/cards";
import Header from "@cloudscape-design/components/header";
import Input from "@cloudscape-design/components/input";
import Modal from "@cloudscape-design/components/modal";
import FormField from "@cloudscape-design/components/form-field";
import SpaceBetween from "@cloudscape-design/components/space-between";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import AppShell from "@/components/AppShell";
import CopyableId from "@/components/CopyableId";
import { usePolling } from "@/hooks/usePolling";
import { api, type Project } from "@/lib/api";

export default function ProjectsPage() {
  const router = useRouter();
  const fetcher = useCallback(() => api.listProjects(), []);
  const { data: projects, loading, refetch } = usePolling(fetcher, 10000);
  const [showCreate, setShowCreate] = useState(false);
  const [newName, setNewName] = useState("");
  const [creating, setCreating] = useState(false);

  const handleCreate = async () => {
    if (!newName.trim()) return;
    setCreating(true);
    try {
      const project = await api.createProject({ name: newName.trim() });
      setShowCreate(false);
      setNewName("");
      refetch();
      router.push(`/projects/${project.id}`);
    } finally {
      setCreating(false);
    }
  };

  return (
    <AppShell
      breadcrumbs={[{ text: "Projects", href: "/projects" }]}
      activeHref="/projects"
    >
      <Cards
        header={
          <Header
            variant="h1"
            actions={
              <Button variant="primary" onClick={() => setShowCreate(true)}>
                Create project
              </Button>
            }
          >
            Projects
          </Header>
        }
        loading={loading}
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
              id: "id",
              header: "ID",
              content: (item) => <CopyableId id={item.id} label="" />,
            },
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
            <Button onClick={() => setShowCreate(true)}>
              Create project
            </Button>
          </Box>
        }
      />

      <Modal
        visible={showCreate}
        onDismiss={() => setShowCreate(false)}
        header="Create project"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setShowCreate(false)}>
                Cancel
              </Button>
              <Button
                variant="primary"
                onClick={handleCreate}
                loading={creating}
              >
                Create
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <FormField label="Project name">
          <Input
            value={newName}
            onChange={({ detail }) => setNewName(detail.value)}
            placeholder="my-project"
          />
        </FormField>
      </Modal>
    </AppShell>
  );
}
