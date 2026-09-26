/*
 * Admin → Updates: the operating system half (spec §21.24 *Updates* "OS
 * upgrades appear as a separate section", §14.4, contracts §5 `GET
 * /system/os`, `POST /system/os/rollback`). An OS package is reviewed and
 * applied from the same drop zone as an application package
 * (`UpdateSection`) — this card is the reading: which slot is active, what
 * the standby holds, and, during a trial, the deadline and what happens if
 * nothing confirms it (Q11).
 *
 * `useOsStatus` answers `isError` on a platform with no A/B root slots
 * (`SlotsUnavailable` — a development host, a port not yet in
 * `porting.md`), and this renders nothing at all rather than an error a
 * development machine can never clear.
 */
import { RotateCcw } from "lucide-react";
import { useState } from "react";

import { Banner } from "@/components/ui/Banner";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { ConfirmDialog } from "@/components/ui/Sheet";

import { useOsStatus, useRollbackOs } from "./api";
import { formatDateTime } from "./format";
import { ReconnectWait } from "./ReconnectWait";
import { type Slot } from "./types";

function slotLabel(slot: Slot | string | null): string {
  return slot === "a" ? "A" : slot === "b" ? "B" : "—";
}

export function OsSection() {
  const statusQuery = useOsStatus();
  const rollback = useRollbackOs();
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [waitMessage, setWaitMessage] = useState<string | null>(null);

  if (statusQuery.isPending || statusQuery.isError) return null;

  const os = statusQuery.data;
  const trial = os.trial;
  const canRollBack = os.standby_version !== null || trial !== null;

  function confirmedRollback(): void {
    setConfirmOpen(false);
    setError(null);
    rollback.mutate(undefined, {
      onSuccess: (result) =>
        setWaitMessage(`Rebooting into slot ${slotLabel(result.slot)}${result.version ? ` (${result.version})` : ""}…`),
      onError: (rollbackError) => setError(rollbackError instanceof Error ? rollbackError.message : "Could not roll back."),
    });
  }

  function reconnected(): void {
    setWaitMessage(null);
  }

  return (
    <Card title="Operating system" titleLevel="h2">
      {waitMessage ? (
        <ReconnectWait message={waitMessage} onReconnected={reconnected} />
      ) : (
        <>
          <dl className="kv">
            <dt>Active slot</dt>
            <dd>
              Slot {slotLabel(os.active_slot)} · <span className="technical">{os.active_version ?? "unknown"}</span>
            </dd>
            <dt>Standby slot</dt>
            <dd>
              Slot {slotLabel(os.standby_slot)} ·{" "}
              <span className="technical">{os.standby_version ?? "empty"}</span>
            </dd>
          </dl>

          {os.pending ? (
            <Banner tone="info" title="An operating system package is ready">
              <span className="technical">{os.pending.version}</span> has been verified. Review it above, in
              Updates — applying it reboots the appliance.
            </Banner>
          ) : null}

          {trial ? (
            <Banner tone="warning" title="On trial">
              Slot {slotLabel(trial.slot)} is running <span className="technical">{trial.version ?? "unknown"}</span> on
              trial. If it is not confirmed healthy by{" "}
              {trial.deadline_at ? formatDateTime(trial.deadline_at) : "the deadline"}, the appliance reboots back
              into the previous operating system automatically.
            </Banner>
          ) : null}

          {error ? (
            <p className="field-note" role="alert">
              {error}
            </p>
          ) : null}

          <div>
            <Button
              variant="secondary"
              confirmTrigger
              helpId="updates.os.rollback"
              disabled={!canRollBack}
              onClick={() => setConfirmOpen(true)}
            >
              <RotateCcw aria-hidden="true" className="size-4" />
              Roll back
            </Button>
          </div>
        </>
      )}

      <ConfirmDialog
        open={confirmOpen}
        onOpenChange={setConfirmOpen}
        title="Roll back the operating system?"
        description={
          trial ? (
            <>
              This reboots the appliance now, cancelling the trial of{" "}
              <span className="technical">{trial.version}</span> and returning to slot{" "}
              {slotLabel(os.active_slot === trial.slot ? os.standby_slot : os.active_slot)}.
            </>
          ) : (
            <>
              This reboots the appliance into slot {slotLabel(os.standby_slot)} (
              <span className="technical">{os.standby_version}</span>), the previous operating system.
            </>
          )
        }
        confirmLabel="Roll back"
        destructive
        onConfirm={confirmedRollback}
      />
    </Card>
  );
}

