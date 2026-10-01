/*
 * §21.24 *Backup*'s "History" block: every archive with its date, size,
 * destination(s) and whether it verified, a download per row, and "Back up
 * now" driving `ProgressPanel` through the real `backup_run` steps (§16.8:
 * never an indeterminate spinner) — the same live-progress pattern
 * `admin/certs/CertificatesScreen.tsx` uses for issuance, because a weekly
 * automatic renewal there and a nightly scheduled backup here are both work
 * that can start without this tab's own button.
 *
 * Two checks, shown apart because they answer different questions (§13.4):
 * "Checked after backup" is the run's own read-back of every copy it wrote
 * ("was it written correctly?"), and "Monthly check" is the random sample
 * that catches later decay on the USB or the NAS ("is it still good?"). An
 * archive the monthly check flagged untrusted is shown as such and offers no
 * restore action — `RestoreCard`'s picker excludes it for the same reason.
 */
import { Check, Database, X } from "lucide-react";

import { ApiError } from "@/api/client";
import { ProgressPanel } from "@/components/ProgressPanel";
import { Banner } from "@/components/ui/Banner";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { formatTime } from "@/lib/time";
import { useProgress } from "@/live/store";

import { downloadUrl, useBackupHistory, useRunBackupNow, useVerifyBackup } from "./api";
import { formatBytes, formatWhen } from "./format";
import { BACKUP_RUN_OPERATION, BACKUP_RUN_STEPS, type ArchiveSummary, type BackupStatus, type BackupVerifyStatus } from "./types";

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

const DESTINATION_LABELS: Record<string, string> = { local: "Local", usb: "USB", network: "Network" };

function CheckedAfterBackup({ archive }: { archive: ArchiveSummary }) {
  if (archive.checked_at === null) {
    return <span className="text-fg-muted text-sm">Not checked (older backup)</span>;
  }
  if (archive.checked_destinations.length === 0) {
    return (
      <span className="inline-flex items-center gap-2 text-danger-text">
        <X aria-hidden="true" strokeWidth={3} className="size-4" />
        No copy read back correctly
      </span>
    );
  }
  const where = archive.checked_destinations.map((d) => DESTINATION_LABELS[d] ?? d).join(", ");
  return (
    <span className="inline-flex items-center gap-2 text-success-text">
      <Check aria-hidden="true" strokeWidth={3} className="size-4" />
      {where} · {formatTime(archive.checked_at)}
    </span>
  );
}

function MonthlyCheck({ archive }: { archive: ArchiveSummary }) {
  if (archive.untrusted) {
    return (
      <span className="inline-flex items-center gap-2 text-danger-text" title={archive.untrusted_reason ?? undefined}>
        <X aria-hidden="true" strokeWidth={3} className="size-4" />
        Failed · untrusted
      </span>
    );
  }
  if (archive.verified_at) {
    return (
      <span className="inline-flex items-center gap-2 text-success-text">
        <Check aria-hidden="true" strokeWidth={3} className="size-4" />
        Passed {formatWhen(archive.verified_at)}
      </span>
    );
  }
  return <span className="text-fg-muted text-sm">Not checked yet</span>;
}

const MONTHLY_RESULT: Record<BackupVerifyStatus["outcome"], string> = {
  verified: "passed",
  none: "nothing to check yet",
  untrusted: "failed",
  missing: "the archive it chose is missing",
  unreachable: "could not reach the archive",
};

function monthlyResult(verify: BackupVerifyStatus): string {
  // A status saved before outcomes existed has none: fall back to pass/fail.
  const outcome = verify.outcome as BackupVerifyStatus["outcome"] | undefined;
  if (outcome === undefined) return verify.ok ? "passed" : "failed";
  return MONTHLY_RESULT[outcome];
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
            Last monthly check {formatWhen(status.last_verify.verified_at)} · {monthlyResult(status.last_verify)}
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
                <th scope="col">Checked after backup</th>
                <th scope="col">Monthly check</th>
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
                    <CheckedAfterBackup archive={archive} />
                  </td>
                  <td>
                    <MonthlyCheck archive={archive} />
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
