/*
 * §21.24 *Backup*'s "Snapshots" block (§18 Phase 7, approved 25 September
 * 2026). Every pre-change, pre-restore and pre-update database snapshot
 * core/snapshots.py has taken, newest first, with when it was taken, why
 * and by whom — from its `.json` sidecar — and a Restore button through the
 * same `POST /system/backup/restore` path `RestoreCard`'s own "Undo this
 * restore" button already uses (`{"snapshot": …}`, §13.2). An older
 * `pre-update-`/`pre-restore-` snapshot from before the sidecar existed is
 * still listed, with only its name and size — nothing here invents a
 * reason or an actor it does not know.
 *
 * A restore from here goes through the identical server-side path
 * `RestoreCard` uses, including its own pre-restore snapshot and restart —
 * but the live progress panel and the restart watch stay `RestoreCard`'s:
 * duplicating that machinery here would be two places racing to show the
 * same restart. Once the restore has been asked for, this card says so and
 * steps back; the restored appliance's own restore record (`status.
 * last_restore`) is what `RestoreCard` shows once the page reconnects,
 * whichever card asked for it.
 */
import { useState } from "react";
import { Camera } from "lucide-react";

import { ApiError } from "@/api/client";
import { Banner } from "@/components/ui/Banner";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { ConfirmDialog } from "@/components/ui/Sheet";

import { useRestoreFromSnapshot, useSnapshots } from "./api";
import { formatBytes, formatWhen } from "./format";
import type { SnapshotListItem } from "./types";

function statusLine(error: unknown): string | undefined {
  return error instanceof ApiError ? `${error.status} ${error.code}` : undefined;
}

function TakenWhen({ snapshot }: { snapshot: SnapshotListItem }) {
  if (snapshot.taken_at === null) return <span className="text-fg-muted">Unknown time</span>;
  return <span>{formatWhen(snapshot.taken_at)}</span>;
}

function TakenWhy({ snapshot }: { snapshot: SnapshotListItem }) {
  if (snapshot.reason === null) {
    return <span className="text-fg-muted text-sm">No record of why — taken before this appliance kept one.</span>;
  }
  const by = snapshot.actor ? ` by ${snapshot.actor}` : "";
  return (
    <span className="text-fg-muted text-sm">
      {snapshot.reason}
      {by}
    </span>
  );
}

export function SnapshotsCard() {
  const snapshots = useSnapshots();
  const restore = useRestoreFromSnapshot();

  const [restoreTarget, setRestoreTarget] = useState<SnapshotListItem | null>(null);
  const [actionError, setActionError] = useState<string | undefined>();
  const [started, setStarted] = useState(false);

  if (snapshots.isPending) {
    return (
      <Card className="device-card" title="Snapshots" titleLevel="h2">
        <p className="text-fg-muted text-sm">Loading…</p>
      </Card>
    );
  }

  if (snapshots.isError || !snapshots.data) {
    return (
      <Card className="device-card" title="Snapshots" titleLevel="h2">
        <Banner tone="danger">Could not load the snapshots. {statusLine(snapshots.error)}</Banner>
      </Card>
    );
  }

  const list = snapshots.data.snapshots;

  function doRestore(): void {
    if (!restoreTarget) return;
    const name = restoreTarget.name;
    setRestoreTarget(null);
    setActionError(undefined);
    restore.mutate(
      { snapshot: name },
      {
        onSuccess: () => setStarted(true),
        onError: (error) => setActionError(error instanceof ApiError ? error.message : "Could not restore this snapshot"),
      },
    );
  }

  return (
    <Card className="device-card" title="Snapshots" titleLevel="h2">
      {actionError ? (
        <p className="field-note" role="alert">
          {actionError}
        </p>
      ) : null}

      {started ? (
        <Banner tone="warning" title="Restoring…">
          The appliance is restarting from this snapshot. This page reconnects automatically once it is back.
        </Banner>
      ) : null}

      {list.length === 0 ? (
        <EmptyState
          icon={Camera}
          title="No snapshots yet"
          detail="One is taken automatically before every destructive change — a delete, a KNX import, a restore or an update."
        />
      ) : (
        <ul className="flex flex-col gap-2">
          {list.map((snapshot) => (
            <li key={snapshot.name} className="flex items-center justify-between gap-3">
              <div>
                <p className="font-medium">
                  <TakenWhen snapshot={snapshot} />
                  {" · "}
                  {formatBytes(snapshot.size_bytes)}
                </p>
                <TakenWhy snapshot={snapshot} />
              </div>
              <Button
                variant="secondary"
                confirmTrigger
                helpId="backup.snapshot.restore"
                disabled={started}
                onClick={() => setRestoreTarget(snapshot)}
              >
                Restore
              </Button>
            </li>
          ))}
        </ul>
      )}

      <ConfirmDialog
        open={restoreTarget !== null}
        onOpenChange={(open) => !open && setRestoreTarget(null)}
        title="Restore this snapshot?"
        description={
          <>
            <p>
              This replaces the <strong>database, venue baselines and TLS certificates</strong> with what this
              snapshot holds — the same replacement a full backup restore makes.
            </p>
            <p>The appliance restarts once the restore finishes. A pre-restore snapshot is taken first, so this is itself reversible.</p>
          </>
        }
        confirmLabel="Restore and restart"
        destructive
        onConfirm={doRestore}
      />
    </Card>
  );
}
