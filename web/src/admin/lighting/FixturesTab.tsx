/*
 * Admin → Lighting → Fixtures (spec §21.18): "a table for bulk work: name,
 * bar, channel, type, groups. Filterable by bar and type. The summary line
 * shows the conflict count in red if any exist." Rows open the fixture sheet
 * — the same one the Stage Plan tab uses (§21.18: one sheet for add and edit).
 */
import { useMemo, useState } from "react";
import { Lightbulb } from "lucide-react";

import { useDevices } from "@/admin/devices/api";
import { ConflictDialog } from "@/admin/devices/ConflictDialog";
import { ApiError } from "@/api/client";
import { presentError } from "@/api/errors";
import { Button } from "@/components/ui/Button";
import { EmptyState, ErrorState, Skeleton } from "@/components/ui/EmptyState";
import { Select } from "@/components/ui/Select";
import { FieldLabel } from "@/help/HelpButton";
import { useLightingChannels, useLightingGroups } from "@/lighting/api";
import type { LightingChannel } from "@/lighting/types";
import { conflictChannelIds } from "@/stageplan/conflicts";
import { useLightingBars, usePatchConflicts } from "@/stageplan/api";
import { FixtureSheet } from "@/stageplan/FixtureSheet";

import { useDeleteFixture, useFixtureProfiles, useFixtureReferences, useKnxAddresses } from "./api";
import { useFixtureSave } from "./useFixtureSave";

function statusLine(error: unknown): string | undefined {
  return error instanceof ApiError ? `${error.status} ${error.code}` : undefined;
}

function typeLabel(channel: LightingChannel, profileName: string | undefined): string {
  return channel.type === "knx_dimmer" ? "KNX dimmer" : (profileName ?? "DMX");
}

export function FixturesTab() {
  const bars = useLightingBars();
  const channels = useLightingChannels();
  const groups = useLightingGroups();
  const profiles = useFixtureProfiles();
  const devices = useDevices();
  const knxAddresses = useKnxAddresses();
  const conflicts = usePatchConflicts();

  const [barFilter, setBarFilter] = useState<number | "all">("all");
  const [typeFilter, setTypeFilter] = useState<"all" | "dmx" | "knx_dimmer">("all");
  const [editing, setEditing] = useState<LightingChannel | null>(null);
  const [sheetSeq, setSheetSeq] = useState(0);

  const deleteFixture = useDeleteFixture();
  const references = useFixtureReferences(editing?.id ?? null);
  const fixtureSave = useFixtureSave(() => {
    setEditing(null);
    setSheetSeq(0);
  });

  const isPending = bars.isPending || channels.isPending || groups.isPending || profiles.isPending || devices.isPending;
  const isError = bars.isError || channels.isError || groups.isError || profiles.isError || devices.isError;

  const allChannels = channels.data?.channels ?? [];
  const allBars = bars.data?.bars ?? [];
  const allGroups = groups.data?.groups ?? [];
  const allProfiles = profiles.data?.profiles ?? [];
  const lightingDevices = (devices.data?.devices ?? []).filter((d) => d.category === "lighting");
  const conflictIds = useMemo(() => conflictChannelIds(conflicts.data?.conflicts ?? []), [conflicts.data]);
  // A rig's worth of fixtures, bars and groups is small (dozens, not
  // thousands) — these lookups are rebuilt each render rather than memoised,
  // which is cheaper than the dependency-array churn from wrapping them.
  const profileById = new Map(allProfiles.map((p) => [p.id, p] as const));
  const barById = new Map(allBars.map((b) => [b.id, b] as const));
  const groupById = new Map(allGroups.map((g) => [g.id, g] as const));

  const filtered = allChannels.filter((channel) => {
    if (barFilter !== "all" && channel.bar_id !== barFilter) return false;
    if (typeFilter !== "all" && channel.type !== typeFilter) return false;
    return true;
  });

  if (isPending) {
    return (
      <div className="flex flex-col gap-4" aria-busy="true" aria-label="Loading fixtures">
        <Skeleton className="h-touch w-full" />
        <Skeleton className="h-touch w-full" />
      </div>
    );
  }

  if (isError) {
    return (
      <ErrorState
        title="Could not load fixtures"
        detail="The controller did not answer. Nothing has been changed."
        status={statusLine(bars.error ?? channels.error ?? groups.error ?? profiles.error ?? devices.error)}
        onRetry={() => {
          void bars.refetch();
          void channels.refetch();
          void groups.refetch();
          void profiles.refetch();
          void devices.refetch();
        }}
      />
    );
  }

  return (
    <div className="lighting-panel">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="flex flex-wrap gap-3">
          <div className="field">
            <FieldLabel htmlFor="fixtures-bar-filter" help="lighting.fixtures.bar-filter">
              Bar
            </FieldLabel>
            <Select
              id="fixtures-bar-filter"
              value={barFilter}
              onChange={(event) => setBarFilter(event.currentTarget.value === "all" ? "all" : Number(event.currentTarget.value))}
            >
              <option value="all">All bars</option>
              {allBars.map((bar) => (
                <option key={bar.id} value={bar.id}>
                  {bar.name}
                </option>
              ))}
            </Select>
          </div>
          <div className="field">
            <FieldLabel htmlFor="fixtures-type-filter" help="lighting.fixtures.type-filter">
              Type
            </FieldLabel>
            <Select
              id="fixtures-type-filter"
              value={typeFilter}
              onChange={(event) => setTypeFilter(event.currentTarget.value as "all" | "dmx" | "knx_dimmer")}
            >
              <option value="all">All types</option>
              <option value="dmx">DMX</option>
              <option value="knx_dimmer">KNX dimmer</option>
            </Select>
          </div>
        </div>
        <Button
          variant="primary"
          helpId="lighting.fixtures.add"
          onClick={() => {
            setEditing(null);
            setSheetSeq((seq) => seq + 1);
          }}
        >
          Add fixture
        </Button>
      </div>

      <p aria-live="polite">
        {filtered.length} fixture{filtered.length === 1 ? "" : "s"}
        {conflictIds.size > 0 ? <span className="lighting-summary-conflict"> · {conflictIds.size} in conflict</span> : null}
      </p>

      {filtered.length === 0 ? (
        <EmptyState icon={Lightbulb} title="No fixtures match" detail="Try a different bar or type filter." />
      ) : (
        <div className="lighting-table-scroll">
          <table className="lighting-table">
            <caption className="sr-only">Every patched fixture</caption>
            <thead>
              <tr>
                <th scope="col">Name</th>
                <th scope="col">Bar</th>
                <th scope="col">Channel</th>
                <th scope="col">Type</th>
                <th scope="col">Groups</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((channel) => {
                const profile = channel.profile_id !== undefined && channel.profile_id !== null ? profileById.get(channel.profile_id) : undefined;
                const memberOf = channel.group_ids.map((id) => groupById.get(id)?.name).filter((name): name is string => Boolean(name));
                return (
                  <tr key={channel.id}>
                    <td>
                      <button
                        type="button"
                        className="btn btn-ghost"
                        onClick={() => {
                          setSheetSeq((seq) => seq + 1);
                          setEditing(channel);
                        }}
                      >
                        {channel.name}
                        {conflictIds.has(channel.id) ? <span className="lighting-summary-conflict"> ⚠</span> : null}
                      </button>
                    </td>
                    <td>{channel.bar_id !== null ? (barById.get(channel.bar_id)?.name ?? "unknown") : "unassigned"}</td>
                    <td className="technical">{channel.type === "dmx" ? (channel.address ?? "—") : "—"}</td>
                    <td>{typeLabel(channel, profile?.name)}</td>
                    <td>{memberOf.length > 0 ? memberOf.join(", ") : "—"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {editing !== null || sheetSeq > 0 ? (
        <FixtureSheet
          key={`fixtures-tab-${sheetSeq}`}
          open={sheetSeq > 0}
          onOpenChange={(open) => {
            if (!open) {
              setEditing(null);
              setSheetSeq(0);
            }
          }}
          fixture={editing}
          bars={allBars}
          profiles={allProfiles}
          devices={lightingDevices}
          knxAddresses={knxAddresses.data ?? []}
          channels={allChannels}
          groups={allGroups}
          saving={fixtureSave.saving}
          onSave={(input) => fixtureSave.save(editing, input)}
          fieldErrors={fixtureSave.fieldErrors}
          deleting={deleteFixture.isPending}
          references={references.data?.references ?? []}
          referencesLoading={references.isPending}
          onDelete={
            editing
              ? () => {
                  const target = editing;
                  deleteFixture.mutate(target.id, {
                    onSuccess: () => {
                      setEditing(null);
                      setSheetSeq(0);
                    },
                    onError: (error) => presentError(error),
                  });
                }
              : undefined
          }
        />
      ) : null}

      {fixtureSave.conflict ? (
        <ConflictDialog
          open
          onOpenChange={(open) => {
            if (!open) fixtureSave.reloadConflict();
          }}
          deviceName={editing?.name ?? "This fixture"}
          rows={[...fixtureSave.conflict.rows]}
          onReload={fixtureSave.reloadConflict}
          onOverwrite={fixtureSave.overwriteConflict}
        />
      ) : null}
    </div>
  );
}
