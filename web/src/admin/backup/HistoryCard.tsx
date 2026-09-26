/*
 * §21.24 *Backup*'s "History" block: every archive with its date, size,
 * destination(s) and whether it verified, a download per row, and "Back up
 * now" driving `ProgressPanel` through the real `backup_run` steps (§16.8:
 * never an indeterminate spinner) — the same live-progress pattern
 * `admin/certs/CertificatesScreen.tsx` uses for issuance, because a weekly
 * automatic renewal there and a nightly scheduled backup here are both work
 * that can start without this tab's own button.
 *
 * An archive the monthly integrity check flagged untrusted (§13.4) is shown
 * as such and offers no restore action — `RestoreCard`'s picker excludes it
 * for the same reason.
 */
import { Check, Database, X } from "lucide-react";

import { ApiError } from "@/api/client";
import { ProgressPanel } from "@/components/ProgressPanel";
import { Banner } from "@/components/ui/Banner";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { useProgress } from "@/live/store";

import { downloadUrl, useBackupHistory, useRunBackupNow, useVerifyBackup } from "./api";
import { formatBytes, formatWhen } from "./format";
import { BACKUP_RUN_OPERATION, BACKUP_RUN_STEPS, type ArchiveSummary, type BackupStatus } from "./types";

function statusLine(error: unknown): string | undefined {
  return error instanceof ApiError ? `${error.status} ${error.code}` : undefined;
}

function DestinationBadges({ archive }: { archive: ArchiveSummary }) {
  const present: string[] = [];
  if (archive.local_present) present.push("Local");
  if (archive.usb_present) present.push("USB");
  if (archive.network_present) present.push("Network");
  if (present.length === 0) return <span className="text-danger-text text-sm">No destination holds this archive</span>;
  return <span className="text-fg-muted text-sm">{present.join(" + ")}</span>;
}

function VerifiedMark({ archive }: { archive: ArchiveSummary }) {
  if (archive.untrusted) {
    return (
      <span className="inline-flex items-center gap-2 text-danger-text" title={archive.untrusted_reason ?? undefined}>
        <X aria-hidden="true" strokeWidth={3} className="size-4" />
        Untrusted
      </span>
    );
  }
  if (archive.verified_at) {
    return (
      <span className="inline-flex items-center gap-2 text-success-text">
        <Check aria-hidden="true" strokeWidth={3} className="size-4" />
        Verified
      </span>
    );
  }
  return <span className="text-fg-muted text-sm">Not yet verified</span>;
}

export interface HistoryCardProps {
  status: BackupStatus | undefined;
}

export function HistoryCard({ status }: HistoryCardProps) {
  const history = useBackupHistory();
  const runNow = useRunBackupNow();
  const verify = useVerifyBackup();

  // Live, not only this tab's own mutation (CertificatesScreen's own reasoning):
  // the nightly 03:00 timer runs the identical job.
  const liveProgress = useProgress(BACKUP_RUN_OPERATION);
  const running = runNow.isPending || (liveProgress !== null && liveProgress.step < liveProgress.of);

  if (history.isPending) {
    return (
      <Card className="device-card" title="History" titleLevel="h2">
        <p className="text-fg-muted text-sm">Loading…</p>
      </Card>
    );
  }

  if (history.isError || !history.data) {
    return (
      <Card className="device-card" title="History" titleLevel="h2">
        <Banner tone="danger">Could not load the backup history. {statusLine(history.error)}</Banner>
      </Card>
    );
  }

  const archives = history.data.archives;

  return (
    <Card className="device-card" title="History" titleLevel="h2">
      <div className="flex flex-wrap items-center gap-4 text-sm">
        {status?.last_run ? (
          <span>
            Last backup {formatWhen(status.last_run.attempted_at)} · {status.last_run.result === "success" ? "succeeded" : "failed"}
          </span>
        ) : (
          <span className="text-fg-muted">No backup has run yet.</span>
        )}
        {status?.last_verify ? (
          <span>
            Last verified {formatWhen(status.last_verify.verified_at)} · {status.last_verify.ok ? "Passed" : "Failed"}
          </span>
        ) : null}
      </div>

      {running ? <ProgressPanel operation={BACKUP_RUN_OPERATION} steps={BACKUP_RUN_STEPS} label="Backup progress" /> : null}

      <div className="flex gap-3">
        <Button variant="primary" helpId="backup.run-now" loading={running} onClick={() => runNow.mutate()}>
          Back up now
        </Button>
        <Button variant="secondary" loading={verify.isPending} onClick={() => verify.mutate()}>
          Verify now
        </Button>
      </div>

      {archives.length === 0 ? (
        <EmptyState icon={Database} title="No backups yet" detail="The first backup runs tonight at 03:00, or start one now." />
      ) : (
        <div className="table-scroll">
          <table className="data-table">
            <caption className="sr-only">Backup history</caption>
            <thead>
              <tr>
                <th scope="col">Date</th>
                <th scope="col">Size</th>
                <th scope="col">Destination</th>
                <th scope="col">Verified</th>
                <th scope="col">
                  <span className="sr-only">Download</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {archives.map((archive) => (
                <tr key={archive.id}>
                  <td>{formatWhen(archive.created_at)}</td>
                  <td>{formatBytes(archive.size_bytes)}</td>
                  <td>
                    <DestinationBadges archive={archive} />
                  </td>
                  <td>
                    <VerifiedMark archive={archive} />
                  </td>
                  <td>
                    <a className="btn btn-secondary btn-icon" href={downloadUrl(archive.id)} download aria-label={`Download the ${formatWhen(archive.created_at)} backup`}>
                      ↓
                    </a>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}
