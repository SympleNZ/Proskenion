/*
 * §21.24 *Backup*'s "Venue baseline" card (§13.5, Q14): when it was captured
 * and what it holds, Capture (replacing the current one, keeping a dated
 * copy — never discarding it, per Q14), Compare (the diff grouped by area),
 * and Restore with its confirmation.
 *
 * Compare is, per §21.24, "the operation that earns its keep on the baseline
 * card" — it renders inline here rather than behind a second navigation, with
 * the same "Restore baseline / Capture current as new baseline / Close"
 * actions the wireframe puts directly on the diff.
 */
import { useState } from "react";
import { GitCompare } from "lucide-react";

import { ApiError } from "@/api/client";
import { Banner } from "@/components/ui/Banner";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { ConfirmDialog } from "@/components/ui/Sheet";

import { useBaselineState, useCaptureBaseline, useCompareBaseline, useRestoreBaseline } from "./api";
import { fieldLabel, formatDate } from "./format";
import type { AreaDiff, BaselineRestoreResult, RowChange } from "./types";
import { BASELINE_AREA_LABELS, BASELINE_AREA_ORDER } from "./types";

function statusLine(error: unknown): string | undefined {
  return error instanceof ApiError ? `${error.status} ${error.code}` : undefined;
}

function contentsLine(contents: Record<string, number>): string {
  const parts = Object.entries(contents)
    .filter(([, count]) => count > 0)
    .map(([key, count]) => `${count} ${fieldLabel(key)}`);
  return parts.length > 0 ? parts.join(" · ") : "Nothing captured yet";
}

function ChangeRow({ change }: { change: RowChange }) {
  return (
    <li className="flex flex-col gap-1 py-2">
      <div className="flex items-center gap-2">
        <span
          className={
            change.change === "added" ? "text-success-text" : change.change === "removed" ? "text-danger-text" : "text-warning-text"
          }
          aria-hidden="true"
        >
          {change.change === "added" ? "+" : change.change === "removed" ? "−" : "~"}
        </span>
        <span className="font-medium">{change.name}</span>
        <span className="sr-only">{change.change}</span>
      </div>
      {change.change === "changed" && change.fields.length > 0 ? (
        <ul className="pl-6 text-sm text-fg-muted flex flex-col gap-0.5">
          {change.fields.map((f) => (
            <li key={f.field}>
              {fieldLabel(f.field)}: {String(f.before ?? "—")} → {String(f.after ?? "—")}
            </li>
          ))}
        </ul>
      ) : null}
    </li>
  );
}

function AreaSection({ area }: { area: AreaDiff }) {
  return (
    <div>
      <h4 className="card-title">{BASELINE_AREA_LABELS[area.area] ?? fieldLabel(area.area)}</h4>
      <ul>
        {area.changes.map((change) => (
          <ChangeRow key={`${change.area}-${change.entity}-${change.id}-${change.change}`} change={change} />
        ))}
      </ul>
    </div>
  );
}

export function BaselineCard() {
  const state = useBaselineState();
  const capture = useCaptureBaseline();
  const restore = useRestoreBaseline();

  const [comparing, setComparing] = useState(false);
  const compare = useCompareBaseline(undefined, comparing);

  const [restoreConfirmOpen, setRestoreConfirmOpen] = useState(false);
  const [restoreFile, setRestoreFile] = useState<string | undefined>(undefined);
  const [restoreError, setRestoreError] = useState<string | undefined>();
  const [missingDevices, setMissingDevices] = useState<string[] | null>(null);
  const [restoreResult, setRestoreResult] = useState<BaselineRestoreResult | null>(null);
  const [captureError, setCaptureError] = useState<string | undefined>();

  if (state.isPending) {
    return (
      <Card className="device-card" title="Venue baseline" titleLevel="h2">
        <p className="text-fg-muted text-sm">Loading…</p>
      </Card>
    );
  }

  if (state.isError || !state.data) {
    return (
      <Card className="device-card" title="Venue baseline" titleLevel="h2">
        <Banner tone="danger">Could not load the venue baseline. {statusLine(state.error)}</Banner>
      </Card>
    );
  }

  const current = state.data.current;
  const areasWithChanges = (compare.data?.areas ?? []).filter((area) => area.changes.length > 0);

  function doCapture(): void {
    setCaptureError(undefined);
    capture.mutate(undefined, {
      onError: (error) => setCaptureError(error instanceof ApiError ? error.message : "Could not capture the baseline"),
    });
  }

  function openRestoreConfirm(file: string | undefined): void {
    setRestoreFile(file);
    setRestoreError(undefined);
    setMissingDevices(null);
    setRestoreConfirmOpen(true);
  }

  function doRestore(): void {
    setRestoreConfirmOpen(false);
    restore.mutate(restoreFile, {
      onSuccess: (result) => {
        setRestoreResult(result);
        setComparing(false);
      },
      onError: (error) => {
        if (error instanceof ApiError && error.code === "conflict" && error.reason === "missing_devices") {
          const devices = error.detail["devices"];
          setMissingDevices(
            Array.isArray(devices)
              ? devices.map((d) => (d && typeof d === "object" && "name" in d ? String((d as { name: unknown }).name ?? "?") : "?"))
              : [],
          );
          return;
        }
        setRestoreError(error instanceof ApiError ? error.message : "Could not restore the baseline");
      },
    });
  }

  return (
    <Card className="device-card" title="Venue baseline" titleLevel="h2">
      {current ? (
        <>
          <dl className="kv">
            <dt>Captured</dt>
            <dd>
              {formatDate(current.captured_at)}
              {current.captured_by ? ` by ${current.captured_by}` : ""}
            </dd>
            <dt>Contains</dt>
            <dd>{contentsLine(current.contents)}</dd>
          </dl>
          {state.data.copies.length > 0 ? (
            <p className="text-fg-muted text-sm">{state.data.copies.length} earlier capture(s) kept, never auto-pruned.</p>
          ) : null}
        </>
      ) : (
        <EmptyState icon={GitCompare} title="No baseline captured yet" detail="Capture one once the venue is configured the way it should be." />
      )}

      {captureError ? (
        <p className="field-note" role="alert">
          {captureError}
        </p>
      ) : null}

      <div className="flex gap-3">
        <Button variant="primary" helpId="backup.baseline.capture" loading={capture.isPending} onClick={doCapture}>
          Capture new baseline
        </Button>
        <Button variant="secondary" disabled={!current} onClick={() => setComparing(true)}>
          Compare
        </Button>
        <Button variant="destructive" helpId="backup.baseline.restore" disabled={!current} onClick={() => openRestoreConfirm(undefined)}>
          Restore
        </Button>
      </div>

      {restoreResult ? (
        <Banner tone="success" title="Baseline restored">
          {Object.entries(restoreResult.restored).reduce((sum, [, n]) => sum + n, 0)} rows applied
          {restoreResult.migrated.length > 0 ? ` (migrated: ${restoreResult.migrated.join(", ")})` : ""}. Reversible from the
          pre-restore snapshot below.
          <div className="mt-2">
            <Button
              variant="secondary"
              loading={restore.isPending}
              onClick={() => openRestoreConfirm(restoreResult.snapshot)}
            >
              Undo this restore
            </Button>
          </div>
        </Banner>
      ) : null}

      {comparing ? (
        <div className="flex flex-col gap-3 border-t border-line pt-3">
          {compare.isPending ? (
            <p className="text-fg-muted text-sm">Comparing…</p>
          ) : compare.isError ? (
            <Banner tone="danger">Could not compare against the baseline. {statusLine(compare.error)}</Banner>
          ) : compare.data ? (
            <>
              <h3 className="card-title">Changes since the baseline captured {formatDate(compare.data.baseline.captured_at)}</h3>
              {compare.data.migrated.length > 0 ? (
                <p className="text-fg-muted text-sm">Migrated forward before comparing: {compare.data.migrated.join(", ")}.</p>
              ) : null}
              {areasWithChanges.length === 0 ? (
                <p>Nothing has drifted since this baseline was captured.</p>
              ) : (
                areasWithChanges
                  .slice()
                  .sort((a, b) => BASELINE_AREA_ORDER.indexOf(a.area) - BASELINE_AREA_ORDER.indexOf(b.area))
                  .map((area) => <AreaSection key={area.area} area={area} />)
              )}
              <div className="flex gap-3">
                <Button variant="destructive" helpId="backup.baseline.restore" onClick={() => openRestoreConfirm(undefined)}>
                  Restore baseline
                </Button>
                <Button variant="secondary" loading={capture.isPending} onClick={doCapture}>
                  Capture current as new baseline
                </Button>
                <Button variant="ghost" onClick={() => setComparing(false)}>
                  Close
                </Button>
              </div>
            </>
          ) : null}
        </div>
      ) : null}

      <ConfirmDialog
        open={restoreConfirmOpen}
        onOpenChange={setRestoreConfirmOpen}
        title={restoreFile ? "Undo this restore?" : "Restore the venue baseline?"}
        description={
          <>
            <p>
              This applies {restoreFile ? "the pre-restore snapshot" : "the captured baseline"} to scenes, lighting,
              KNX, rules, mixer, video, pages and hirer permissions — never devices, which a baseline cannot
              recreate.
            </p>
            <p>A snapshot of the live configuration is taken first, so this is itself reversible.</p>
          </>
        }
        confirmLabel={restoreFile ? "Undo" : "Restore"}
        destructive
        onConfirm={doRestore}
      />

      {restoreError ? (
        <p className="field-note" role="alert">
          {restoreError}
        </p>
      ) : null}

      {missingDevices ? (
        <Banner tone="warning" title="This baseline needs devices this appliance no longer has">
          Recreate {missingDevices.join(", ")} on the Devices screen, then restore again.
        </Banner>
      ) : null}
    </Card>
  );
}
