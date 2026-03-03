"use client";

import { useState } from "react";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import FormField from "@cloudscape-design/components/form-field";
import Input from "@cloudscape-design/components/input";
import Modal from "@cloudscape-design/components/modal";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Alert from "@cloudscape-design/components/alert";

interface ConfirmDeleteModalProps {
  visible: boolean;
  onDismiss: () => void;
  onConfirm: () => Promise<void> | void;
  resourceName: string;
  resourceType: string;
}

export default function ConfirmDeleteModal({
  visible,
  onDismiss,
  onConfirm,
  resourceName,
  resourceType,
}: ConfirmDeleteModalProps) {
  const [confirmText, setConfirmText] = useState("");
  const [deleting, setDeleting] = useState(false);

  const matches = confirmText === resourceName;

  const handleConfirm = async () => {
    setDeleting(true);
    try {
      await onConfirm();
    } finally {
      setDeleting(false);
      setConfirmText("");
    }
  };

  const handleDismiss = () => {
    setConfirmText("");
    onDismiss();
  };

  return (
    <Modal
      visible={visible}
      onDismiss={handleDismiss}
      header={`Delete ${resourceType}`}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={handleDismiss}>
              Cancel
            </Button>
            <span className="btn-danger">
              <Button
                variant="primary"
                onClick={handleConfirm}
                disabled={!matches}
                loading={deleting}
              >
                Delete
              </Button>
            </span>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="m">
        <Alert type="warning">
          This will permanently delete the {resourceType}{" "}
          <strong>{resourceName}</strong> and all associated data. This action
          cannot be undone.
        </Alert>
        <FormField
          label={
            <>
              Type <strong>{resourceName}</strong> to confirm
            </>
          }
        >
          <Input
            value={confirmText}
            onChange={({ detail }) => setConfirmText(detail.value)}
            placeholder={resourceName}
          />
        </FormField>
      </SpaceBetween>
    </Modal>
  );
}
