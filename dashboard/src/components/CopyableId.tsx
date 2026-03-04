"use client";

import { useState } from "react";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Popover from "@cloudscape-design/components/popover";

interface CopyableIdProps {
  /** The ID value to display and copy */
  id: string;
  /** Optional label shown before the ID (defaults to "ID") */
  label?: string;
}

/**
 * Displays a truncated ID with a one-click copy button.
 * Shows the full ID on hover via a popover, and flashes
 * a "Copied!" confirmation after clicking the copy icon.
 */
export default function CopyableId({ id, label = "ID" }: CopyableIdProps) {
  const [copied, setCopied] = useState(false);

  const handleCopy = () => {
    navigator.clipboard.writeText(id);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  // Show first 8 chars of UUID-style IDs for brevity
  const shortId = id.length > 12 ? id.slice(0, 8) + "…" : id;

  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 4,
      }}
    >
      <Box variant="small" color="text-body-secondary" display="inline-block">
        {label}:
      </Box>
      <Popover
        dismissButton={false}
        position="top"
        size="small"
        triggerType="custom"
        content={<Box variant="small">{copied ? "Copied!" : id}</Box>}
      >
        <span
          style={{ cursor: "pointer", fontFamily: "monospace", fontSize: "12px" }}
          title={id}
          onClick={handleCopy}
        >
          {shortId}
        </span>
      </Popover>
      <Button
        variant="inline-icon"
        iconName="copy"
        ariaLabel={`Copy ${label}`}
        onClick={handleCopy}
      />
    </span>
  );
}
