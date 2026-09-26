/*
 * §21.24 *Backup*'s "Restore" block: upload a backup file, or pick one
 * already held on this appliance, then the confirmations before anything is
 * touched. Q15 is stated plainly before the button that starts it — what a
 * restore replaces (the database, baselines and certificates) and what it
 * leaves alone (network settings, the token, application code) — and the
 * server's own `network_differences` are shown, never applied, after it runs.
 *
 * A restore that succeeds restarts the appliance (`RestoreResponse.restarted`)
 * before this process can say anything else about it. This card asks the
 * public `/health` when it is back (`useRestartWatch`) and then says so, with
 * the time it restarted. The restore itself is recorded in the restored
 * database, so after a refresh the card still shows the last restore — when,
 * from what, and which passwords are still needed — until it is dismissed.
 */
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { Upload } from "lucide-react";

import { noteCertificateChange, useCertificateChange } from "@/api/certificateChange";
import { ApiError } from "@/api/client";
import { CertificateChangedBanner } from "@/components/CertificateChangedBanner";
import { ProgressPanel } from "@/components/ProgressPanel";
import { Banner } from "@/components/ui/Banner";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { Field, Input } from "@/components/ui/Input";
import { ConfirmDialog, Sheet, SheetContent } from "@/components/ui/Sheet";
import { useProgress } from "@/live/store";
import { formatTime } from "@/lib/time";

import {
  backupKeys,
  useAcknowledgeRestore,
  useBackupHistory,
  useBackupStatus,
  useRestartWatch,
  useRestoreFromArchive,
  useRestoreFromSnapshot,
  useRestoreFromUpload,
} from "./api";
import { formatBytes, formatWhen } from "./format";
import {
  BACKUP_RESTORE_ARCHIVE_STEPS,
  BACKUP_RESTORE_OPERATION,
  type ArchiveSummary,
  type BackupRestoreResult,
  type LastRestore,
} from "./types";

type Source = { kind: "upload"; file: File; sha256: string } | { kind: "archive"; archive: ArchiveSummary };

function sourceLabel(source: Source): string {
  return source.kind === "upload"
    ? `${source.file.name} (${formatBytes(source.file.size)})`
    : `the ${formatWhen(source.archive.created_at)} backup (${formatBytes(source.archive.size_bytes)})`;
}

function ReplacesNotice() {
  return (
    <>
      <p>
        This replaces the <strong>database, venue baselines and TLS certificates</strong> with what is in the backup.
      </p>
      <p>
        It never touches <strong>network settings, this appliance&apos;s Cloudflare token, or the application code</strong>{" "}
        — a difference the backup&apos;s network settings show against the current ones is reported, not applied.
      </p>
      <p>The appliance restarts once the restore finishes. A pre-restore snapshot is taken first, so this is itself reversible.</p>
    </>
  );
}

/**
 * Whether the appliance is back is asked of `/health` (`useRestartWatch`),
 * never read off the live socket. The socket was the signal once, and the
 * banner never cleared on the rebuilt appliance (25 September 2026): its
 * reconnection is refused at the upgrade whenever the restored database's
 * `token_version` differs from the session's — as it does after any restore
 * onto a freshly set-up appliance — and a refused upgrade looks to the
 * browser like the network, so it retries for ever. And when the restore
 * brought a different certificate back, nothing on this page reaches the
 * controller until the new certificate is accepted.
 */
function RestartBanner({ back, restartedAt, certificateChanged }: { back: boolean; restartedAt: Date | null; certificateChanged: boolean }) {
  if (back) {
    return (
      <Banner tone="success" title={`Restore complete — the controller restarted at ${restartedAt ? formatTime(restartedAt, { seconds: true }) : "just now"}`}>
        Sign in again if asked: the restored database carries the passwords it was backed up with.
      </Banner>
    );
  }
  if (certificateChanged) {
    return (
      <Banner tone="warning" title="Restarting the appliance…">
        This page cannot confirm the restart until the new certificate has been accepted (above).
      </Banner>
    );
  }
  return (
    <Banner tone="warning" title="Restarting the appliance…">
      This page will reconnect automatically once it is back.
    </Banner>
  );
}

function sourceText(source: BackupRestoreResult["source"]): string {
  switch (source) {
    case "upload":
      return "the uploaded file";
    case "local":
      return "this appliance's local copy";
    case "usb":
      return "the backup USB";
    case "network":
      return "the network destination";
    case "snapshot":
      return "a pre-restore snapshot";
  }
}

function RestoreResult({ result, respondedAt, pollMs }: { result: BackupRestoreResult; respondedAt: number; pollMs?: number | undefined }) {
  const undo = useRestoreFromSnapshot();
  const client = useQueryClient();
  const certificateChange = useCertificateChange();
  const watch = useRestartWatch(result.restarted ? respondedAt : null, pollMs);
  const [undone, setUndone] = useState(false);
  const back = result.restarted && watch.back;

  useEffect(() => {
    if (!back) return;
    // The restore record and the history are the restored database's now.
    void client.invalidateQueries({ queryKey: backupKeys.status });
    void client.invalidateQueries({ queryKey: backupKeys.history });
  }, [back, client]);

  return (
    <div className="flex flex-col gap-3 border-t border-line pt-3">
      {result.certificate_replaced && certificateChange ? <CertificateChangedBanner change={certificateChange} /> : null}
      <p className="font-medium">Restored from {sourceText(result.source)}.</p>
      <dl className="kv">
        <dt>Replaced</dt>
        <dd>{result.replaced.length > 0 ? result.replaced.join(", ") : "—"}</dd>
        <dt>Not applied</dt>
        <dd>{result.not_applied.length > 0 ? result.not_applied.join(", ") : "—"}</dd>
      </dl>
      {result.network_differences.length > 0 ? (
        <div>
          <p className="field-label">Network settings the backup disagrees with (not applied)</p>
          <table className="data-table">
            <thead>
              <tr>
                <th scope="col">Setting</th>
                <th scope="col">Current</th>
                <th scope="col">In the backup</th>
              </tr>
            </thead>
            <tbody>
              {result.network_differences.map((diff) => (
                <tr key={diff.key}>
                  <td>{diff.key}</td>
                  <td className="technical">{String(diff.current ?? "—")}</td>
                  <td className="technical">{String(diff.archived ?? "—")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      {result.device_passwords_require_reentry ? (
        <Banner tone="warning">
          This restore came from a different appliance — device passwords must be re-entered on the Devices screen
          before those drivers reconnect{result.devices_needing_passwords.length > 0 ? `: ${result.devices_needing_passwords.join(", ")}` : "."}
        </Banner>
      ) : null}
      {result.migrations_pending.length > 0 ? (
        <p className="text-fg-muted text-sm">The next start migrates the restored database forward: {result.migrations_pending.join(", ")}.</p>
      ) : null}

      {result.settings_needing_passwords.length > 0 ? (
        <Banner tone="warning">
          These passwords were encrypted by a different appliance and must be re-entered:{" "}
          {result.settings_needing_passwords.join(", ")}.
        </Banner>
      ) : null}

      {result.restarted ? (
        <RestartBanner back={back} restartedAt={watch.restartedAt} certificateChanged={result.certificate_replaced && certificateChange !== null} />
      ) : null}

      {back && !undone ? (
        <div>
          <Button
            variant="secondary"
            loading={undo.isPending}
            onClick={() =>
              undo.mutate(
                { snapshot: result.snapshot },
                {
                  onSuccess: () => setUndone(true),
                },
              )
            }
          >
            Undo this restore
          </Button>
        </div>
      ) : null}
      {undone ? <Banner tone="success">Undone — the pre-restore snapshot was restored, and the appliance is restarting again.</Banner> : null}
    </div>
  );
}

function attentionText(last: LastRestore): string {
  const needed = [...last.devices_still_needing_passwords, ...last.settings_still_needing_passwords];
  if (needed.length === 0) return "nothing needs attention";
  return `${needed.join(", ")} ${needed.length === 1 ? "password" : "passwords"} needed`;
}

/**
 * The last restore, as the restored database records it (§21.24) — shown
 * after a refresh, until an administrator dismisses it or another restore
 * supersedes it. Before this, a refresh lost every trace of the restore.
 */
function LastRestoreSummary({ last }: { last: LastRestore }) {
  const acknowledge = useAcknowledgeRestore();
  const from = last.archive_id ? `${sourceText(last.source)} ${last.archive_id}` : sourceText(last.source);
  return (
    <section className="flex flex-col gap-2 border-b border-line pb-3" aria-label="Last restore">
      <p>
        <strong>Last restore:</strong> {formatWhen(last.at)} from {from} — {attentionText(last)}
      </p>
      {last.restarted_at ? (
        <p className="text-fg-muted text-sm">The controller restarted on the restored database at {formatWhen(last.restarted_at)}.</p>
      ) : null}
      {last.devices_still_needing_passwords.length > 0 ? (
        <p className="text-fg-muted text-sm">Device passwords are re-entered on Admin → Devices.</p>
      ) : null}
      <div>
        <Button variant="secondary" loading={acknowledge.isPending} onClick={() => acknowledge.mutate()}>
          Dismiss
        </Button>
      </div>
    </section>
  );
}

export interface RestoreCardProps {
  /** How often to ask `/health` whether the appliance is back; tests shorten it. */
  pollMs?: number;
}

export function RestoreCard({ pollMs }: RestoreCardProps = {}) {
  const history = useBackupHistory();
  const status = useBackupStatus();
  const fromUpload = useRestoreFromUpload();
  const fromArchive = useRestoreFromArchive();
  const fileInput = useRef<HTMLInputElement>(null);

  const [pickerOpen, setPickerOpen] = useState(false);
  const [source, setSource] = useState<Source | null>(null);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [error, setError] = useState<string | undefined>();
  const [result, setResult] = useState<BackupRestoreResult | null>(null);
  const [respondedAt, setRespondedAt] = useState(0);

  function restored(data: BackupRestoreResult): void {
    setRespondedAt(Date.now());
    setResult(data);
    if (data.certificate_replaced) noteCertificateChange(data.certificate_names);
  }

  const last = status.data?.last_restore ?? null;
  const showLast = last !== null && last.acknowledged_at === null && (result === null || result.at !== last.at);

  const liveProgress = useProgress(BACKUP_RESTORE_OPERATION);
  const running = fromUpload.isPending || fromArchive.isPending || (liveProgress !== null && liveProgress.step < liveProgress.of);

  function onFileChosen(file: File): void {
    setSource({ kind: "upload", file, sha256: "" });
    setResult(null);
    setError(undefined);
  }

  function pickArchive(archive: ArchiveSummary): void {
    setSource({ kind: "archive", archive });
    setPickerOpen(false);
    setResult(null);
    setError(undefined);
  }

  function setSha256(value: string): void {
    setSource((prev) => (prev && prev.kind === "upload" ? { ...prev, sha256: value } : prev));
  }

  function doRestore(): void {
    if (!source) return;
    setConfirmOpen(false);
    setError(undefined);
    if (source.kind === "upload") {
      const sha256 = source.sha256.trim();
      fromUpload.mutate(
        sha256 ? { file: source.file, sha256 } : { file: source.file },
        {
          onSuccess: restored,
          onError: (err) => setError(err instanceof ApiError ? err.message : "Could not restore from this file"),
        },
      );
    } else {
      fromArchive.mutate(
        { archive_id: source.archive.id },
        {
          onSuccess: restored,
          onError: (err) => setError(err instanceof ApiError ? err.message : "Could not restore from this backup"),
        },
      );
    }
  }

  const archives = history.data?.archives ?? [];

  return (
    <Card className="device-card" title="Restore" titleLevel="h2">
      {showLast ? <LastRestoreSummary last={last} /> : null}
      <div className="flex gap-3">
        <input
          ref={fileInput}
          type="file"
          accept=".tar.zst,.zst"
          className="sr-only"
          aria-label="Upload a backup file"
          onChange={(e) => {
            const file = e.currentTarget.files?.[0];
            if (file) onFileChosen(file);
            e.currentTarget.value = "";
          }}
        />
        <Button variant="secondary" onClick={() => fileInput.current?.click()}>
          <Upload aria-hidden="true" className="size-4" />
          Upload a backup file
        </Button>
        <Button variant="secondary" onClick={() => setPickerOpen(true)}>
          Restore from backup media
        </Button>
      </div>

      {source ? (
        <div className="flex flex-col gap-3 border-t border-line pt-3">
          <p>
            Selected: <strong>{sourceLabel(source)}</strong>
          </p>
          {source.kind === "upload" ? (
            <Field label="SHA-256 (optional, from the .sha256 sidecar)" htmlFor="restore-sha256" helpId="backup.restore.sha256" errorId="restore-sha256-error">
              <Input id="restore-sha256" mono value={source.sha256} onChange={(e) => setSha256(e.currentTarget.value)} />
            </Field>
          ) : null}
          {error ? (
            <p className="field-note" role="alert">
              {error}
            </p>
          ) : null}
          {running ? (
            <ProgressPanel operation={BACKUP_RESTORE_OPERATION} steps={BACKUP_RESTORE_ARCHIVE_STEPS} label="Restore progress" />
          ) : (
            <div>
              <Button variant="destructive" helpId="backup.restore.confirm" loading={running} onClick={() => setConfirmOpen(true)}>
                Restore from this backup
              </Button>
            </div>
          )}
        </div>
      ) : null}

      {result ? <RestoreResult result={result} respondedAt={respondedAt} pollMs={pollMs} /> : null}

      <ConfirmDialog
        open={confirmOpen}
        onOpenChange={setConfirmOpen}
        title="Restore the appliance from this backup?"
        description={<ReplacesNotice />}
        confirmLabel="Restore and restart"
        destructive
        onConfirm={doRestore}
      />

      <Sheet open={pickerOpen} onOpenChange={setPickerOpen}>
        <SheetContent title="Restore from backup media">
          {archives.length === 0 ? (
            <p className="text-fg-muted text-sm">No backups are held on this appliance yet.</p>
          ) : (
            <ul className="flex flex-col gap-2">
              {archives.map((archive) => (
                <li key={archive.id} className="flex items-center justify-between gap-3">
                  <div>
                    <p>{formatWhen(archive.created_at)}</p>
                    <p className="text-fg-muted text-sm">{formatBytes(archive.size_bytes)}</p>
                  </div>
                  {archive.untrusted ? (
                    <span className="text-danger-text text-sm">Untrusted — cannot restore</span>
                  ) : (
                    <Button variant="secondary" onClick={() => pickArchive(archive)}>
                      Restore
                    </Button>
                  )}
                </li>
              ))}
            </ul>
          )}
        </SheetContent>
      </Sheet>
    </Card>
  );
}
