/*
 * §21.24 *Backup*'s "System images" block (§13.6, Q13). Capture is manual
 * and deliberate; Restore never overwrites the running slot — it writes the
 * image into the standby slot and boots it on trial (§14.4), exactly as an
 * OS upgrade does, so this screen says plainly that it reboots. Restoring
 * onto a replacement SSD is not offered here at all — that is the recovery
 * environment's job (§13.7) — so every row's Restore acts on this same
 * machine, never a new one.
 *
 * The USB's capacity rule (Q4): images are evicted before archives, so a
 * full stick never costs a backup to make room for an image.
 */
import { useState } from "react";
import { HardDrive } from "lucide-react";

import { ApiError } from "@/api/client";
import { ProgressPanel } from "@/components/ProgressPanel";
import { Banner } from "@/components/ui/Banner";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { ConfirmDialog } from "@/components/ui/Sheet";
import { useProgress } from "@/live/store";

import { useCaptureImage, useDeleteImage, useImages, useRestoreImage } from "./api";
import { formatAge, formatBytes } from "./format";
import { IMAGE_CAPTURE_OPERATION, IMAGE_CAPTURE_STEPS, IMAGE_RESTORE_OPERATION, IMAGE_RESTORE_STEPS, type SystemImage } from "./types";

function statusLine(error: unknown): string | undefined {
  return error instanceof ApiError ? `${error.status} ${error.code}` : undefined;
}

export function ImagesCard() {
  const images = useImages();
  const capture = useCaptureImage();
  const restore = useRestoreImage();
  const del = useDeleteImage();

  const [restoreTarget, setRestoreTarget] = useState<SystemImage | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<SystemImage | null>(null);
  const [actionError, setActionError] = useState<string | undefined>();
  const [restoring, setRestoring] = useState<string | null>(null);

  const captureProgress = useProgress(IMAGE_CAPTURE_OPERATION);
  const capturing = capture.isPending || (captureProgress !== null && captureProgress.step < captureProgress.of);
  const restoreProgress = useProgress(IMAGE_RESTORE_OPERATION);
  const restoringLive = restoring !== null && (restore.isPending || (restoreProgress !== null && restoreProgress.step < restoreProgress.of));

  if (images.isPending) {
    return (
      <Card className="device-card" title="System images" titleLevel="h2">
        <p className="text-fg-muted text-sm">Loading…</p>
      </Card>
    );
  }

  if (images.isError || !images.data) {
    return (
      <Card className="device-card" title="System images" titleLevel="h2">
        <Banner tone="danger">Could not load the system images. {statusLine(images.error)}</Banner>
      </Card>
    );
  }

  const list = images.data.images;

  function doCapture(): void {
    setActionError(undefined);
    capture.mutate(undefined, {
      onError: (error) => setActionError(error instanceof ApiError ? error.message : "Could not capture an image"),
    });
  }

  function doRestore(): void {
    if (!restoreTarget) return;
    const id = restoreTarget.id;
    setRestoring(id);
    setActionError(undefined);
    setRestoreTarget(null);
    restore.mutate(id, {
      onError: (error) => {
        setRestoring(null);
        setActionError(error instanceof ApiError ? error.message : "Could not restore this image");
      },
    });
  }

  function doDelete(): void {
    if (!deleteTarget) return;
    const id = deleteTarget.id;
    setDeleteTarget(null);
    del.mutate(id, {
      onError: (error) => setActionError(error instanceof ApiError ? error.message : "Could not delete this image"),
    });
  }

  return (
    <Card className="device-card" title="System images" titleLevel="h2">
      {actionError ? (
        <p className="field-note" role="alert">
          {actionError}
        </p>
      ) : null}

      {capturing ? (
        <ProgressPanel operation={IMAGE_CAPTURE_OPERATION} steps={IMAGE_CAPTURE_STEPS} label="Image capture progress" />
      ) : (
        <div>
          <Button variant="secondary" loading={capturing} onClick={doCapture}>
            Capture new image
          </Button>
        </div>
      )}

      {restoringLive ? <ProgressPanel operation={IMAGE_RESTORE_OPERATION} steps={IMAGE_RESTORE_STEPS} label="Image restore progress" /> : null}

      {list.length === 0 ? (
        <EmptyState
          icon={HardDrive}
          title="No system images yet"
          detail="Capture one after initial setup, after a significant OS-level change, or before an OS upgrade."
        />
      ) : (
        <ul className="flex flex-col gap-2">
          {list.map((image) => (
            <li key={image.id} className="flex items-center justify-between gap-3">
              <div>
                <p className="font-medium">{image.id}</p>
                <p className="text-fg-muted text-sm">
                  {formatAge(image.created_at)} · {formatBytes(image.size_bytes)}
                  {image.local_present && image.usb_present ? " · local + USB" : image.usb_present ? " · USB" : " · local"}
                </p>
              </div>
              <div className="flex gap-2">
                <Button
                  variant="secondary"
                  confirmTrigger
                  helpId="backup.image.restore"
                  loading={restoringLive && restoring === image.id}
                  disabled={restoringLive}
                  onClick={() => setRestoreTarget(image)}
                >
                  Restore
                </Button>
                <Button
                  variant="ghost"
                  confirmTrigger
                  helpId="backup.image.delete"
                  disabled={restoringLive}
                  onClick={() => setDeleteTarget(image)}
                >
                  Delete
                </Button>
              </div>
            </li>
          ))}
        </ul>
      )}

      <ConfirmDialog
        open={restoreTarget !== null}
        onOpenChange={(open) => !open && setRestoreTarget(null)}
        title="Restore this system image?"
        description={
          <>
            <p>
              This writes the image into the <strong>standby slot</strong> and boots it on trial — the running slot
              is never overwritten while it is running. The appliance reboots to do it, and confirms itself healthy
              after ten minutes; a slot that never confirms falls back automatically.
            </p>
            <p>Only images this machine itself captured can be restored this way.</p>
          </>
        }
        confirmLabel="Restore and reboot"
        destructive
        onConfirm={doRestore}
      />

      <ConfirmDialog
        open={deleteTarget !== null}
        onOpenChange={(open) => !open && setDeleteTarget(null)}
        title="Delete this system image?"
        description="This frees the space it held. It cannot be undone."
        confirmLabel="Delete"
        destructive
        onConfirm={doDelete}
      />
    </Card>
  );
}
