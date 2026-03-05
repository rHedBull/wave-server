"use client";

import { use, useCallback, useState } from "react";
import { useRouter } from "next/navigation";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Cards from "@cloudscape-design/components/cards";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import Input from "@cloudscape-design/components/input";
import Modal from "@cloudscape-design/components/modal";
import FormField from "@cloudscape-design/components/form-field";
import SpaceBetween from "@cloudscape-design/components/space-between";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import AppShell from "@/components/AppShell";
import ConfirmDeleteModal from "@/components/ConfirmDeleteModal";
import CopyableId from "@/components/CopyableId";
import { usePolling } from "@/hooks/usePolling";
import { api, type Project, type Sequence } from "@/lib/api";

function statusType(status: string) {
  switch (status) {
    case "completed":
      return "success";
    case "failed":
      return "error";
    case "running":
      return "in-progress";
    case "cancelled":
      return "stopped";
    default:
      return "pending";
  }
}

export default function ProjectDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const router = useRouter();

  const projectFetcher = useCallback(() => api.getProject(id), [id]);
  const sequenceFetcher = useCallback(() => api.listSequences(id), [id]);

  const { data: project } = usePolling(projectFetcher, 30000);
  const {
    data: sequences,
    loading: seqLoading,
    refetch: refetchSeqs,
  } = usePolling(sequenceFetcher, 5000);

  const [showCreate, setShowCreate] = useState(false);
  const [newName, setNewName] = useState("");
  const [creating, setCreating] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<Sequence | null>(null);

  const handleCreate = async () => {
    if (!newName.trim()) return;
    setCreating(true);
    try {
      const seq = await api.createSequence(id, { name: newName.trim() });
      setShowCreate(false);
      setNewName("");
      refetchSeqs();
      router.push(`/sequences/${seq.id}`);
    } finally {
      setCreating(false);
    }
  };

  if (!project) return null;

  return (
    <AppShell
      breadcrumbs={[
        { text: "Projects", href: "/projects" },
        { text: project.name, href: `/projects/${id}` },
      ]}
      activeHref={`/projects/${id}`}
    >
      <SpaceBetween size="l">
        <Header
          variant="h1"
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              <Button onClick={() => router.push(`/projects/${id}/settings`)}>
                Settings
              </Button>
              <Button variant="primary" onClick={() => setShowCreate(true)}>
                Create sequence
              </Button>
            </SpaceBetween>
          }
        >
          {project.name}
        </Header>

        {project.description && (
          <Container>
            <Box variant="p">{project.description}</Box>
          </Container>
        )}

        <Cards
          header={<Header variant="h2">Sequences</Header>}
          loading={seqLoading}
          loadingText="Loading sequences"
          items={sequences || []}
          cardDefinition={{
            header: (item) => (
              <Box fontWeight="bold">
                <a
                  href={`/sequences/${item.id}`}
                  onClick={(e) => {
                    e.preventDefault();
                    router.push(`/sequences/${item.id}`);
                  }}
                  style={{ textDecoration: "none", color: "inherit" }}
                >
                  {item.name}
                </a>
              </Box>
            ),
            sections: [
              {
                id: "status",
                header: "Status",
                content: (item) => (
                  <StatusIndicator type={statusType(item.status)}>
                    {item.status}
                  </StatusIndicator>
                ),
              },
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
                id: "stats",
                header: "Stats",
                content: (item) => {
                  const parts = [];
                  if (item.wave_count != null) parts.push(`${item.wave_count} waves`);
                  if (item.task_count != null) parts.push(`${item.task_count} tasks`);
                  return parts.length > 0 ? parts.join(", ") : "—";
                },
              },
              {
                id: "actions",
                content: (item) => (
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      setDeleteTarget(item);
                    }}
                    style={{
                      background: "none",
                      border: "none",
                      color: "#d91515",
                      cursor: "pointer",
                      padding: 0,
                      fontSize: "inherit",
                      fontFamily: "inherit",
                    }}
                  >
                    Delete
                  </button>
                ),
              },
            ],
          }}
          empty={
            <Box textAlign="center" color="inherit" padding="l">
              <b>No sequences</b>
              <Box padding={{ bottom: "s" }} variant="p" color="inherit">
                Create a sequence to start orchestrating.
              </Box>
              <Button onClick={() => setShowCreate(true)}>
                Create sequence
              </Button>
            </Box>
          }
        />
      </SpaceBetween>

      <Modal
        visible={showCreate}
        onDismiss={() => setShowCreate(false)}
        header="Create sequence"
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
        <FormField label="Sequence name">
          <Input
            value={newName}
            onChange={({ detail }) => setNewName(detail.value)}
            placeholder="add-oauth"
          />
        </FormField>
      </Modal>

      {deleteTarget && (
        <ConfirmDeleteModal
          visible={!!deleteTarget}
          onDismiss={() => setDeleteTarget(null)}
          onConfirm={async () => {
            await api.deleteSequence(deleteTarget.id);
            setDeleteTarget(null);
            refetchSeqs();
          }}
          resourceName={deleteTarget.name}
          resourceType="sequence"
        />
      )}
    </AppShell>
  );
}
