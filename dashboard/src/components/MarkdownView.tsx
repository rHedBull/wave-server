"use client";

import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import Box from "@cloudscape-design/components/box";

interface MarkdownViewProps {
  title: string;
  content: string | null;
}

export default function MarkdownView({ title, content }: MarkdownViewProps) {
  return (
    <Container header={<Header variant="h3">{title}</Header>}>
      {content ? (
        <Box variant="code">
          <pre style={{ whiteSpace: "pre-wrap", margin: 0, fontSize: "13px" }}>
            {content}
          </pre>
        </Box>
      ) : (
        <Box color="text-status-inactive">Not uploaded yet</Box>
      )}
    </Container>
  );
}
