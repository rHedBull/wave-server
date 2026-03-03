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
import { api, ProjectRepository, ProjectContextFile } from "@/lib/api";

export default function ProjectSettingsPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const router = useRouter();

  const fetcher = useCallback(() => api.getProject(id), [id]);
  const { data: project, refetch } = usePolling(fetcher, 60000);

  const repoFetcher = useCallback(() => api.listRepositories(id), [id]);
  const { data: repos, refetch: refetchRepos } = usePolling(repoFetcher, 60000);

  const ctxFetcher = useCallback(() => api.listContextFiles(id), [id]);
  const { data: contextFiles, refetch: refetchCtx } = usePolling(ctxFetcher, 60000);

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [saving, setSaving] = useState(false);
  const [initialized, setInitialized] = useState(false);

  const [repoPath, setRepoPath] = useState("");
  const [repoLabel, setRepoLabel] = useState("");
  const [ctxPath, setCtxPath] = useState("");
  const [ctxDesc, setCtxDesc] = useState("");

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

        <Container header={<Header variant="h2">Repositories</Header>}>
          <SpaceBetween size="s">
            {repos && repos.length > 0 ? (
              repos.map((repo: ProjectRepository, idx: number) => (
                <SpaceBetween key={repo.id} size="xs" direction="horizontal">
                  <Box variant="code">{repo.path}</Box>
                  {repo.label && <Box variant="small">{repo.label}</Box>}
                  {idx === 0 && <Box variant="small">primary</Box>}
                  <Button
                    variant="icon"
                    iconName="close"
                    onClick={async () => {
                      await api.deleteRepository(id, repo.id);
                      refetchRepos();
                    }}
                  />
                </SpaceBetween>
              ))
            ) : (
              <Box color="text-status-inactive">No repositories linked.</Box>
            )}
            <SpaceBetween size="xs" direction="horizontal">
              <FormField label="Path">
                <Input
                  value={repoPath}
                  onChange={({ detail }) => setRepoPath(detail.value)}
                  placeholder="/path/to/repo"
                />
              </FormField>
              <FormField label="Label (optional)">
                <Input
                  value={repoLabel}
                  onChange={({ detail }) => setRepoLabel(detail.value)}
                />
              </FormField>
              <Box padding={{ top: "xxl" }}>
                <Button
                  onClick={async () => {
                    if (!repoPath.trim()) return;
                    await api.addRepository(id, {
                      path: repoPath.trim(),
                      label: repoLabel.trim() || undefined,
                    });
                    setRepoPath("");
                    setRepoLabel("");
                    refetchRepos();
                  }}
                >
                  Add
                </Button>
              </Box>
            </SpaceBetween>
          </SpaceBetween>
        </Container>

        <Container header={<Header variant="h2">Context Files</Header>}>
          <SpaceBetween size="s">
            {contextFiles && contextFiles.length > 0 ? (
              contextFiles.map((cf: ProjectContextFile) => (
                <SpaceBetween key={cf.id} size="xs" direction="horizontal">
                  <Box variant="code">{cf.path}</Box>
                  {cf.description && <Box variant="small">{cf.description}</Box>}
                  <Button
                    variant="icon"
                    iconName="close"
                    onClick={async () => {
                      await api.deleteContextFile(id, cf.id);
                      refetchCtx();
                    }}
                  />
                </SpaceBetween>
              ))
            ) : (
              <Box color="text-status-inactive">No context files linked.</Box>
            )}
            <SpaceBetween size="xs" direction="horizontal">
              <FormField label="Path">
                <Input
                  value={ctxPath}
                  onChange={({ detail }) => setCtxPath(detail.value)}
                  placeholder="/path/to/file"
                />
              </FormField>
              <FormField label="Description (optional)">
                <Input
                  value={ctxDesc}
                  onChange={({ detail }) => setCtxDesc(detail.value)}
                />
              </FormField>
              <Box padding={{ top: "xxl" }}>
                <Button
                  onClick={async () => {
                    if (!ctxPath.trim()) return;
                    await api.addContextFile(id, {
                      path: ctxPath.trim(),
                      description: ctxDesc.trim() || undefined,
                    });
                    setCtxPath("");
                    setCtxDesc("");
                    refetchCtx();
                  }}
                >
                  Add
                </Button>
              </Box>
            </SpaceBetween>
          </SpaceBetween>
        </Container>

        <Container header={<Header variant="h2">Danger zone</Header>}>
          <Button onClick={handleDelete}>Delete project</Button>
        </Container>
      </SpaceBetween>
    </AppShell>
  );
}
