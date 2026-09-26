/*
 * Admin → Mixer → Channels (§21.21): "the virtual surface... Inserting a
 * channel mid-list is normal." Unmapped channels — left over after a driver
 * change (§5.5) — are not controllable and are shown first, with a warning,
 * rather than hidden.
 */
import { useState } from "react";
import { TriangleAlert } from "lucide-react";

import { ApiError } from "@/api/client";
import { presentError } from "@/api/errors";
import { Banner } from "@/components/ui/Banner";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { ConfirmDialog } from "@/components/ui/Sheet";
import { formatDb, type FaderLawPoint } from "@/lib/faderLaw";

import type { ChannelBody } from "./api";
import { useCreateChannel, useDeleteChannel, useUpdateChannel } from "./api";
import { ChannelSheet } from "./ChannelSheet";
import { ReferenceGuard } from "./ReferenceGuard";
import type { ChannelRef, MixerChannel, Reference } from "./types";

function mapsTo(channel: MixerChannel, refs: readonly ChannelRef[]): string {
  if (channel.driver_refs.length === 0) return "—";
  return channel.driver_refs.map((code) => refs.find((ref) => ref.ref === code)?.label ?? code).join(" + ");
}

/** Unmapped first (§5.5 "appear at the top of the list with a warning"), then the venue's own order. */
function orderedRows(channels: readonly MixerChannel[]): MixerChannel[] {
  return [...channels].sort((a, b) => {
    if (a.unmapped !== b.unmapped) return a.unmapped ? -1 : 1;
    return a.sort_order - b.sort_order;
  });
}

export interface ChannelsSectionProps {
  deviceId: number;
  channels: readonly MixerChannel[];
  refs: readonly ChannelRef[];
  faderLaw: readonly FaderLawPoint[];
}

export function ChannelsSection({ deviceId, channels, refs, faderLaw }: ChannelsSectionProps) {
  const [sheet, setSheet] = useState<{ channel: MixerChannel | null } | null>(null);
  const [sheetSeq, setSheetSeq] = useState(0);
  const [deleteTarget, setDeleteTarget] = useState<MixerChannel | null>(null);
  const [guard, setGuard] = useState<{ name: string; references: Reference[] } | null>(null);
  const [conflict, setConflict] = useState<{ current: MixerChannel } | null>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});

  const create = useCreateChannel();
  const update = useUpdateChannel();
  const remove = useDeleteChannel();

  const inputRefs = refs.filter((ref) => ref.kind === "input");
  const rows = orderedRows(channels);
  const unmappedCount = rows.filter((channel) => channel.unmapped).length;

  function save(body: ChannelBody, version?: string): void {
    setErrors({});
    const editing = sheet?.channel;
    if (editing) {
      update.mutate(
        { id: editing.id, version: version ?? editing.updated_at, body },
        { onSuccess: () => { setSheet(null); setConflict(null); }, onError: handleSaveError },
      );
    } else {
      create.mutate(body, { onSuccess: () => setSheet(null), onError: handleSaveError });
    }
  }

  function handleSaveError(error: unknown): void {
    if (error instanceof ApiError) {
      if (error.code === "conflict") {
        const current = error.detail["current"] as MixerChannel | undefined;
        if (current) {
          setConflict({ current });
          return;
        }
      }
      if (error.code === "validation_failed") {
        const presentation = presentError(error);
        if (presentation?.kind === "inline") setErrors(presentation.fields);
        return;
      }
    }
    presentError(error);
  }

  function requestDelete(channel: MixerChannel): void {
    remove.mutate(channel.id, {
      onError: (error) => {
        if (error instanceof ApiError && error.code === "in_use") {
          const references = (error.detail["references"] as Reference[] | undefined) ?? [];
          setGuard({ name: channel.name, references });
          return;
        }
        presentError(error);
      },
    });
  }

  return (
    <section className="device-group" aria-labelledby="mixer-channels-heading">
      <div className="view-head">
        <h2 className="section-title" id="mixer-channels-heading">
          Channels
        </h2>
        <Button
          variant="primary"
          helpId="mixer.channels.add"
          onClick={() => {
            setSheetSeq((n) => n + 1);
            setErrors({});
            setSheet({ channel: null });
          }}
        >
          + Add channel
        </Button>
      </div>

      {unmappedCount > 0 ? (
        <Banner tone="warning">
          {unmappedCount} channel{unmappedCount === 1 ? "" : "s"} lost its reference in a driver change and needs remapping. An unmapped channel is
          not controllable and is excluded from hirer access until fixed (§5.5).
        </Banner>
      ) : null}

      {rows.length === 0 ? (
        <p className="metric-note">No channels yet. A venue exposing four of twenty inputs should add those four, not delete the other sixteen.</p>
      ) : (
        <Card className="device-card" compact>
          <div className="table-scroll">
            <table className="data-table">
              <caption className="sr-only">The venue&apos;s input channels</caption>
              <thead>
                <tr>
                  <th scope="col">Name</th>
                  <th scope="col">Maps to</th>
                  <th scope="col">Staff</th>
                  <th scope="col">Max</th>
                  <th scope="col">Tracked</th>
                  <th scope="col">
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {rows.map((channel) => (
                  <tr key={channel.id} data-unmapped={channel.unmapped || undefined}>
                    <td>
                      {channel.name}
                      {channel.unmapped ? (
                        <span className="pill" data-tone="warning">
                          <TriangleAlert aria-hidden="true" className="size-3" /> Unmapped
                        </span>
                      ) : null}
                    </td>
                    <td className={channel.unmapped ? "text-fg-muted" : "technical"}>{mapsTo(channel, refs)}</td>
                    <td>{channel.visible_staff ? "Yes" : "No"}</td>
                    <td className="technical">{channel.hirer_max_db === null ? "—" : formatDb(channel.hirer_max_db)}</td>
                    <td>{channel.tracked ? "Yes" : "No"}</td>
                    <td className="device-actions">
                      <Button
                        variant="secondary"
                        onClick={() => {
                          setSheetSeq((n) => n + 1);
                          setErrors({});
                          setSheet({ channel });
                        }}
                      >
                        Edit
                      </Button>
                      <Button variant="destructive" helpId="mixer.channel.delete" onClick={() => setDeleteTarget(channel)} loading={remove.isPending}>
                        Delete
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}

      {sheet ? (
        <ChannelSheet
          key={`mixer-channel-${sheetSeq}`}
          open
          onOpenChange={(open) => !open && setSheet(null)}
          kind="input"
          deviceId={deviceId}
          channel={sheet.channel ?? undefined}
          availableRefs={inputRefs}
          faderLaw={faderLaw}
          defaultSortOrder={rows.length}
          saving={create.isPending || update.isPending}
          errors={errors}
          onSave={(body, version) => save(body, version)}
          conflict={conflict}
          onConflictReload={() => {
            setConflict(null);
            setSheet(null);
          }}
          onConflictDismiss={() => setConflict(null)}
        />
      ) : null}

      <ConfirmDialog
        open={deleteTarget !== null}
        onOpenChange={(open) => !open && setDeleteTarget(null)}
        title={`Delete "${deleteTarget?.name}"?`}
        description="Refused while a scene action still targets it."
        confirmLabel="Delete"
        destructive
        onConfirm={() => {
          const target = deleteTarget;
          setDeleteTarget(null);
          if (target) requestDelete(target);
        }}
      />

      <ReferenceGuard
        open={guard !== null}
        onOpenChange={(next) => {
          if (!next) setGuard(null);
        }}
        subjectName={guard?.name ?? ""}
        references={guard?.references ?? []}
      />
    </section>
  );
}
