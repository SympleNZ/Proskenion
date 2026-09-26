/*
 * Admin → Updates: the application half (spec §21.24 *Updates*, §14.1–§14.3,
 * contracts §3, §5, §6). One drop zone for a `.aupkg`, whichever kind it
 * turns out to be (an OS package is routed to the same upload endpoint —
 * `manifest.type` decides what the review card offers next); the verified
 * manifest for review; apply now or at a quiet moment; and roll back.
 *
 * A rejected package is never guessed at (contracts §3): the sentence shown
 * is exactly the server's `summary` for whichever rule refused it, and a bad
 * signature reads the same as a damaged file because the appliance cannot
 * tell them apart either.
 */
import { useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { PackageCheck, RotateCcw, UploadCloud } from "lucide-react";

import { ApiError, NetworkError } from "@/api/client";
import { ProgressPanel } from "@/components/ProgressPanel";
import { Banner } from "@/components/ui/Banner";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { ErrorState, Skeleton } from "@/components/ui/EmptyState";
import { ConfirmDialog } from "@/components/ui/Sheet";

import { type ApplyWhen, updateKeys, useApplyUpdate, useDiscardUpdate, useRollbackUpdate, useUpdateStatus } from "./api";
import { formatBytes, formatDate, formatDateTime } from "./format";
import { QuietConditions } from "./QuietConditions";
import { ReconnectWait } from "./ReconnectWait";
import { APPLY_OPERATION, APPLY_STEPS } from "./types";
import { type UploadProgress, uploadPackage } from "./upload";

function statusLine(error: unknown): string | undefined {
  return error instanceof ApiError ? `${error.status} ${error.code}` : undefined;
}

function messageOf(error: unknown, fallback: string): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof NetworkError) return error.message;
  return fallback;
}

type ApplyChoice = "now" | "quiet" | "os";

export function UpdateSection() {
  const statusQuery = useUpdateStatus();
  const client = useQueryClient();
  const discard = useDiscardUpdate();
  const apply = useApplyUpdate();
  const rollback = useRollbackUpdate();

  const fileInput = useRef<HTMLInputElement>(null);
  const abortRef = useRef<AbortController | null>(null);

  const [dragActive, setDragActive] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [uploadProgress, setUploadProgress] = useState<UploadProgress | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [applyError, setApplyError] = useState<string | null>(null);
  const [rollbackError, setRollbackError] = useState<string | null>(null);
  const [confirmApply, setConfirmApply] = useState<ApplyChoice | null>(null);
  const [confirmRollback, setConfirmRollback] = useState(false);
  const [waitMessage, setWaitMessage] = useState<string | null>(null);

  if (statusQuery.isPending) {
    return (
      <Card title="Updates" titleLevel="h2" aria-busy="true" aria-label="Loading update status">
        <Skeleton className="h-touch w-full" />
      </Card>
    );
  }

  if (statusQuery.isError) {
    return (
      <Card title="Updates" titleLevel="h2">
        <ErrorState
          title="Could not load update status"
          status={statusLine(statusQuery.error)}
          onRetry={() => void statusQuery.refetch()}
        />
      </Card>
    );
  }

  const status = statusQuery.data;
  const pending = status.pending;
  const previousVersion = status.previous_versions[0] ?? null;

  async function handleFiles(files: FileList | null): Promise<void> {
    const file = files?.[0];
    if (!file || uploading) return;
    setUploadError(null);
    setUploading(true);
    setUploadProgress({ loaded: 0, total: file.size });
    const controller = new AbortController();
    abortRef.current = controller;
    try {
      await uploadPackage(file, {
        signal: controller.signal,
        onProgress: setUploadProgress,
      });
      await client.invalidateQueries({ queryKey: updateKeys.status });
      await client.invalidateQueries({ queryKey: updateKeys.os });
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") {
        // Cancelled — nothing was refused, so there is nothing to say.
      } else {
        setUploadError(messageOf(error, "This package could not be uploaded."));
      }
    } finally {
      setUploading(false);
      setUploadProgress(null);
      abortRef.current = null;
    }
  }

  function cancelUpload(): void {
    abortRef.current?.abort();
  }

  function openPicker(): void {
    if (uploading) return;
    fileInput.current?.click();
  }

  function doDiscard(): void {
    setUploadError(null);
    setApplyError(null);
    discard.mutate();
  }

  function confirmedApply(): void {
    const choice = confirmApply;
    setConfirmApply(null);
    if (choice === null) return;
    const when: ApplyWhen = choice === "quiet" ? "quiet" : "now";
    setApplyError(null);
    apply.mutate(when, {
      onSuccess: (result) => {
        if (result.state === "applied") {
          setWaitMessage(`Restarting the application to ${result.applied?.to_version ?? "the new version"}…`);
        } else if (result.state === "os_trial") {
          setWaitMessage(`Rebooting into ${result.trial?.version ?? "the new operating system"} on trial…`);
        }
      },
      onError: (error) => setApplyError(messageOf(error, "Could not apply the update.")),
    });
  }

  function confirmedRollback(): void {
    setConfirmRollback(false);
    setRollbackError(null);
    rollback.mutate(undefined, {
      onSuccess: (result) => setWaitMessage(`Rolling back to ${result.to_version}…`),
      onError: (error) => setRollbackError(messageOf(error, "Could not roll back.")),
    });
  }

  function reconnected(): void {
    setWaitMessage(null);
    void client.invalidateQueries({ queryKey: updateKeys.status });
    void client.invalidateQueries({ queryKey: updateKeys.os });
  }

  const isOsPackage = pending?.manifest.type === "os";
  const applying = status.state === "applying" || apply.isPending;
  const armed = status.state === "waiting_for_quiet";

  return (
    <Card title="Updates" titleLevel="h2">
      <p className="text-fg-secondary text-sm">
        Installed version <span className="technical">{status.installed_version ?? "unknown"}</span>
      </p>

      {status.rolled_back ? (
        <Banner tone="danger" title="An update was rolled back automatically">
          <span className="technical">{status.rolled_back.from_version}</span> failed to start on{" "}
          {formatDateTime(status.rolled_back.at)}. The appliance restored{" "}
          <span className="technical">{status.rolled_back.to_version}</span> and the database snapshot taken
          before the update, and is running that version now. Review the logs before retrying.
        </Banner>
      ) : null}

      {waitMessage ? (
        <ReconnectWait message={waitMessage} onReconnected={reconnected} />
      ) : applying ? (
        <ProgressPanel operation={APPLY_OPERATION} steps={APPLY_STEPS} label="Update apply progress" />
      ) : status.state === "failed" && status.error ? (
        <Banner tone="danger" title="The update could not be applied">
          {status.error}
        </Banner>
      ) : null}

      {!waitMessage && !applying && pending ? (
        <div className="flex flex-col gap-3">
          <div className="flex items-start gap-3">
            <PackageCheck aria-hidden="true" className="text-success-text size-6 shrink-0" />
            <div>
              <p className="font-medium text-fg">Package verified</p>
              <p className="text-fg-secondary text-sm">
                {isOsPackage ? "Operating system package" : "Application package"} ·{" "}
                <span className="technical">{pending.manifest.version}</span>
              </p>
            </div>
          </div>
          <dl className="kv">
            <dt>Built</dt>
            <dd>{formatDate(pending.manifest.created_at)}</dd>
            {pending.manifest.min_app_version ? (
              <>
                <dt>Minimum required</dt>
                <dd className="technical">{pending.manifest.min_app_version}</dd>
              </>
            ) : null}
          </dl>
          {pending.manifest.changes.length > 0 ? (
            <div>
              <p className="sect-label">Changes</p>
              <ul className="flex flex-col gap-1 text-sm">
                {pending.manifest.changes.map((change) => (
                  <li key={change}>{change}</li>
                ))}
              </ul>
            </div>
          ) : null}

          {isOsPackage ? (
            <Banner tone="warning" title="Applying this reboots the appliance">
              The appliance reboots immediately into the new operating system on trial. If it is not healthy
              within ten minutes, or nothing confirms it, it reboots back into the current system automatically
              (§14.4).
            </Banner>
          ) : (
            <div>
              <p className="sect-label">The next quiet moment applies when</p>
              <QuietConditions quiet={status.quiet} />
            </div>
          )}

          {applyError ? (
            <p className="field-note" role="alert">
              {applyError}
            </p>
          ) : null}

          {armed ? (
            <div className="flex items-center gap-3">
              <Banner tone="info" className="flex-1" title="Armed — waiting for a quiet moment">
                Applies automatically once every condition above holds. Checked once a minute.
              </Banner>
              <Button variant="secondary" loading={discard.isPending} onClick={doDiscard}>
                Cancel
              </Button>
            </div>
          ) : (
            <div className="flex flex-wrap gap-3">
              {isOsPackage ? (
                <Button variant="primary" helpId="updates.apply-os" onClick={() => setConfirmApply("os")}>
                  Apply and reboot
                </Button>
              ) : (
                <>
                  <Button variant="primary" helpId="updates.apply-now" onClick={() => setConfirmApply("now")}>
                    Apply now
                  </Button>
                  <Button variant="secondary" onClick={() => setConfirmApply("quiet")}>
                    Apply at a quiet moment
                  </Button>
                </>
              )}
              <Button variant="ghost" loading={discard.isPending} onClick={doDiscard}>
                Discard
              </Button>
            </div>
          )}
        </div>
      ) : null}

      {!waitMessage && !applying && !pending ? (
        <div className="flex flex-col gap-3">
          {uploading ? (
            <div className="upload-progress" role="status" aria-live="polite">
              <p className="text-sm text-fg">
                Uploading… {formatBytes(uploadProgress?.loaded ?? 0)}
                {uploadProgress?.total ? ` of ${formatBytes(uploadProgress.total)}` : ""}
              </p>
              <div className="upload-progress-bar">
                <div
                  className="upload-progress-fill"
                  style={{
                    width:
                      uploadProgress?.total && uploadProgress.total > 0
                        ? `${Math.min(100, (uploadProgress.loaded / uploadProgress.total) * 100)}%`
                        : "100%",
                  }}
                />
              </div>
              <Button variant="ghost" onClick={cancelUpload}>
                Cancel upload
              </Button>
            </div>
          ) : (
            <>
              {/* Purely a programmatic trigger for openPicker() — kept outside
                  the dropzone's own role="button" rather than nested inside
                  it. A focusable, unlabelled input nested in another
                  interactive element confuses screen readers even hidden
                  behind tabIndex/aria-hidden — axe's own advice is that a
                  negative tabIndex does not stop assistive tech reaching it
                  (§24.3: "no interactive element below…" applies to sole,
                  not doubled-up, controls). */}
              <input
                ref={fileInput}
                type="file"
                accept=".aupkg"
                className="hidden"
                aria-label="Choose an update package"
                onChange={(event) => {
                  void handleFiles(event.currentTarget.files);
                  event.currentTarget.value = "";
                }}
              />
              <div
                className="dropzone"
                data-active={dragActive}
                role="button"
                tabIndex={0}
                aria-label="Upload an update package"
                onClick={openPicker}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    openPicker();
                  }
                }}
                onDragOver={(event) => {
                  event.preventDefault();
                  setDragActive(true);
                }}
                onDragLeave={() => setDragActive(false)}
                onDrop={(event) => {
                  event.preventDefault();
                  setDragActive(false);
                  void handleFiles(event.dataTransfer.files);
                }}
              >
                <UploadCloud aria-hidden="true" className="size-8" />
                <p>Drop a .aupkg package here, or click to choose one</p>
              </div>
            </>
          )}

          {uploadError ? (
            <Banner tone="danger" title="This package was refused">
              {uploadError}
            </Banner>
          ) : null}
        </div>
      ) : null}

      {!waitMessage && !applying && previousVersion ? (
        <div className="flex items-center gap-3 border-t border-hairline pt-3">
          <p className="text-fg-secondary text-sm flex-1">
            Previous version <span className="technical">{previousVersion}</span> available
          </p>
          <Button variant="secondary" onClick={() => setConfirmRollback(true)}>
            <RotateCcw aria-hidden="true" className="size-4" />
            Roll back
          </Button>
        </div>
      ) : null}

      {rollbackError ? (
        <p className="field-note" role="alert">
          {rollbackError}
        </p>
      ) : null}

      <ConfirmDialog
        open={confirmApply === "now"}
        onOpenChange={(open) => !open && setConfirmApply(null)}
        title="Apply now?"
        description={
          <>
            This restarts the Proskenion application to{" "}
            <span className="technical">{pending?.manifest.version}</span>. It will be unavailable for about a
            minute, and everyone connected will be disconnected.
          </>
        }
        confirmLabel="Apply now"
        onConfirm={confirmedApply}
      />
      <ConfirmDialog
        open={confirmApply === "quiet"}
        onOpenChange={(open) => !open && setConfirmApply(null)}
        title="Apply at the next quiet moment?"
        description="The update is armed and applies as soon as hirer access is off, no scene is running, nobody has been connected for ten minutes, and it is outside 02:30–03:30 — checked once a minute. A blue banner shows it is waiting."
        confirmLabel="Arm the update"
        onConfirm={confirmedApply}
      />
      <ConfirmDialog
        open={confirmApply === "os"}
        onOpenChange={(open) => !open && setConfirmApply(null)}
        title="Apply and reboot?"
        description={
          <>
            This reboots the appliance immediately into{" "}
            <span className="technical">{pending?.manifest.version}</span> on trial. If it is not healthy within
            ten minutes, or nothing confirms it, the appliance reboots back into the current system automatically.
          </>
        }
        confirmLabel="Apply and reboot"
        destructive
        onConfirm={confirmedApply}
      />
      <ConfirmDialog
        open={confirmRollback}
        onOpenChange={setConfirmRollback}
        title="Roll back?"
        description={
          <>
            This restores <span className="technical">{previousVersion}</span> and the database exactly as it
            was immediately before the update to <span className="technical">{status.installed_version}</span>.
            Anything changed since — new scenes, configuration, log entries — will be lost. The application
            restarts.
          </>
        }
        confirmLabel="Roll back"
        destructive
        onConfirm={confirmedRollback}
      />
    </Card>
  );
}
