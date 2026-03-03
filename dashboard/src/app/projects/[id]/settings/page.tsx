"use client";

import { use, useCallback, useState } from "react";
import { useRouter } from "next/navigation";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Container from "@cloudscape-design/components/container";
import Form from "@cloudscape-design/components/form";
import FormField from "@cloudscape-design/components/form-field";
import Header from "@cloudscape-design/components/header";
import Input from "@cloudscape-design/components/input";
import SpaceBetween from "@cloudscape-design/components/space-between";
import AppShell from "@/components/AppShell";
import { usePolling } from "@/hooks/usePolling";
import { api } from "@/lib/api";

export default function ProjectSettingsPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const router = useRouter();

  const fetcher = useCallback(() => api.getProject(id), [id]);
  const { data: project, refetch } = usePolling(fetcher, 60000);

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [saving, setSaving] = useState(false);
  const [initialized, setInitialized] = useState(false);

  if (project && !initialized) {
    setName(project.name);
    setDescription(project.description || "");
    setInitialized(true);
  }

  const handleSave = async () => {
    setSaving(true);
    try {
      await api.updateProject(id, { name, description });
      refetch();
    } finally {
      setSaving(false);
    }
  };

  const handleRegenerate = async () => {
    await api.regenerateKey(id);
    refetch();
  };

  const handleDelete = async () => {
    await api.deleteProject(id);
    router.push("/projects");
  };

  if (!project) return null;

  return (
    <AppShell
      breadcrumbs={[
        { text: "Projects", href: "/projects" },
        { text: project.name, href: `/projects/${id}` },
        { text: "Settings", href: `/projects/${id}/settings` },
      ]}
    >
      <SpaceBetween size="l">
        <Header variant="h1">Settings</Header>

        <Form
          actions={
            <Button variant="primary" onClick={handleSave} loading={saving}>
              Save
            </Button>
          }
        >
          <Container header={<Header variant="h2">General</Header>}>
            <SpaceBetween size="l">
              <FormField label="Name">
                <Input
                  value={name}
                  onChange={({ detail }) => setName(detail.value)}
                />
              </FormField>
              <FormField label="Description">
                <Input
                  value={description}
                  onChange={({ detail }) => setDescription(detail.value)}
                />
              </FormField>
            </SpaceBetween>
          </Container>
        </Form>

        <Container header={<Header variant="h2">API Key</Header>}>
          <SpaceBetween size="s">
            <Box variant="code">{project.api_key}</Box>
            <Button onClick={handleRegenerate}>Regenerate key</Button>
          </SpaceBetween>
        </Container>

        <Container header={<Header variant="h2">Danger zone</Header>}>
          <Button onClick={handleDelete}>Delete project</Button>
        </Container>
      </SpaceBetween>
    </AppShell>
  );
}
