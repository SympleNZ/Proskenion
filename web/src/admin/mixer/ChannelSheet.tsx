/*
 * Add and edit one output or input channel (§21.21, §7.3, §5.5). Outputs and
 * inputs share one shape — `mixer_channels` plus its ordered
 * `mixer_channel_refs` — so one sheet serves both; `kind` only changes which
 * refs the picker offers and whether pan is offered at all (outputs have no
 * pan on the CQ-20B, §7.3 "What MIDI reaches").
 *
 * §21.21's mock-up draws the "Linked" choice as a checkbox with a tooltip.
 * With the CQ-20B driver, link state cannot be read back over MIDI (§7.3
 * *Linked stereo outputs*), so there is nothing a checkbox could reflect —
 * the admin's intent has to be *which reference*, not a flag layered on top
 * of one. Picking the pair's own reference (`out12`, labelled "Out 1/2
 * (linked)") from `available_refs()` says the same thing the checkbox would
 * have, without inventing a boolean the driver cannot confirm. The warning
 * text stays, next to the picker, verbatim from §7.3.
 */
import { useState } from "react";

import { ConflictDialog } from "@/admin/devices/ConflictDialog";
import { diffRecord, type ConflictRow } from "@/admin/lighting/diff";
import { Banner } from "@/components/ui/Banner";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Checkbox } from "@/components/ui/Select";
import { Sheet, SheetContent } from "@/components/ui/Sheet";
import { FieldLabel } from "@/help/HelpButton";
import type { FaderLawPoint } from "@/lib/faderLaw";
import { saveFormOnShortcut } from "@/lib/keyboard";

import type { ChannelBody } from "./api";
import { HirerCeilingControl } from "./HirerCeilingControl";
import { RefPicker } from "./RefPicker";
import type { ChannelKind, ChannelRef, MixerChannel } from "./types";

/** The scribble strip takes at most 7 characters; `null` means "truncate the name" (§5.5, §21.21). */
const SCRIBBLE_LENGTH = 7;
/** About sixteen characters fit a channel strip without wrapping (§5.5, §21.21). */
const STRIP_LENGTH = 16;

function truncateScribble(name: string): string {
  return name.slice(0, SCRIBBLE_LENGTH);
}

export interface ChannelSheetProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  kind: Exclude<ChannelKind, "main">;
  deviceId: number;
  /** `undefined` — Add; otherwise the channel being edited. */
  channel?: MixerChannel | undefined;
  /** Already restricted to `kind` by the caller. */
  availableRefs: readonly ChannelRef[];
  faderLaw: readonly FaderLawPoint[];
  defaultSortOrder: number;
  saving: boolean;
  onSave: (body: ChannelBody, version?: string) => void;
  conflict: { current: MixerChannel } | null;
  onConflictReload: () => void;
  onConflictDismiss: () => void;
  errors: Record<string, string>;
}

export function ChannelSheet({
  open,
  onOpenChange,
  kind,
  deviceId,
  channel,
  availableRefs,
  faderLaw,
  defaultSortOrder,
  saving,
  onSave,
  conflict,
  onConflictReload,
  onConflictDismiss,
  errors,
}: ChannelSheetProps) {
  const [name, setName] = useState(channel?.name ?? "");
  const [shortNameOverride, setShortNameOverride] = useState(channel?.short_name ?? "");
  const [notes, setNotes] = useState(channel?.notes ?? "");
  const [driverRefs, setDriverRefs] = useState<string[]>(channel?.driver_refs ?? []);
  const [visibleStaff, setVisibleStaff] = useState(channel?.visible_staff ?? true);
  const [hirerMaxDb, setHirerMaxDb] = useState<number | null>(channel?.hirer_max_db ?? null);
  const [showPan, setShowPan] = useState(channel?.show_pan ?? false);
  const [tracked, setTracked] = useState(channel?.tracked ?? true);
  const [sortOrder, setSortOrder] = useState(channel?.sort_order ?? defaultSortOrder);
  const [localError, setLocalError] = useState<string | undefined>();

  const noun = kind === "input" ? "channel" : "output";
  const effectiveShortName = shortNameOverride.trim() || truncateScribble(name);
  const fitsStrip = name.length <= STRIP_LENGTH;

  function buildBody(): ChannelBody {
    return {
      device_id: deviceId,
      channel_kind: kind,
      name: name.trim(),
      short_name: shortNameOverride.trim() || null,
      notes: notes.trim() || null,
      driver_refs: driverRefs,
      visible_staff: visibleStaff,
      hirer_max_db: hirerMaxDb,
      show_pan: kind === "input" ? showPan : false,
      tracked,
      sort_order: sortOrder,
    };
  }

  function submit(overrideVersion?: string): void {
    if (!name.trim()) {
      setLocalError("Give it a name");
      return;
    }
    if (driverRefs.length === 0) {
      setLocalError(`Choose which physical ${kind === "input" ? "input" : "output"} this maps to`);
      return;
    }
    setLocalError(undefined);
    onSave(buildBody(), overrideVersion);
  }

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent
        title={channel ? `Edit "${channel.name}"` : `Add ${kind === "input" ? "a channel" : "an output"}`}
        description={
          kind === "input"
            ? "The virtual surface does not have to mirror the desk's own numbering (§5.5)."
            : "Mono, or a linked pair chosen from the desk's own paired references (§7.3)."
        }
      >
        <form
          className="sheet-body"
          noValidate
          onKeyDown={saveFormOnShortcut}
          onSubmit={(event) => {
            event.preventDefault();
            submit();
          }}
        >
          <div className="field schema-field">
            <FieldLabel htmlFor={`mixer-${kind}-name`} help="mixer.channel.name">
              Name
            </FieldLabel>
            <Input id={`mixer-${kind}-name`} value={name} onChange={(event) => setName(event.currentTarget.value)} autoFocus />
            <p className="field-help">
              {name.length} / ~{STRIP_LENGTH} characters
              {fitsStrip ? " — fits the strip" : " — names never wrap; put the rest in notes (§5.5)"}
            </p>
          </div>

          <div className="field schema-field">
            <FieldLabel htmlFor={`mixer-${kind}-short-name`} help="mixer.channel.short-name">
              Scribble strip override
            </FieldLabel>
            <Input
              id={`mixer-${kind}-short-name`}
              value={shortNameOverride}
              placeholder={truncateScribble(name) || "—"}
              maxLength={SCRIBBLE_LENGTH}
              onChange={(event) => setShortNameOverride(event.currentTarget.value)}
            />
            <p className="field-help">
              Shows as &quot;{effectiveShortName || "—"}&quot; on the control surface&apos;s 7-character scribble strip (§7.6). Only needed when
              truncation collides with another channel&apos;s.
            </p>
          </div>

          <RefPicker idPrefix={`mixer-${kind}`} refs={availableRefs} value={driverRefs} onChange={setDriverRefs} />
          {kind === "output" ? (
            <Banner tone="warning">
              Set this to match the stereo link state configured on the CQ in MixPad. The mixer does not report link state over MIDI (§7.3).
            </Banner>
          ) : null}

          <HirerCeilingControl id={`mixer-${kind}-hirer-max`} value={hirerMaxDb} onChange={setHirerMaxDb} law={faderLaw} />

          <div className="flex flex-wrap gap-4">
            <Checkbox
              id={`mixer-${kind}-staff`}
              label="Visible to staff"
              checked={visibleStaff}
              onChange={(event) => setVisibleStaff(event.currentTarget.checked)}
            />
            <Checkbox
              id={`mixer-${kind}-tracked`}
              label="Track state"
              checked={tracked}
              onChange={(event) => setTracked(event.currentTarget.checked)}
            />
            {kind === "input" ? (
              <Checkbox
                id={`mixer-${kind}-pan`}
                label="Show pan"
                checked={showPan}
                onChange={(event) => setShowPan(event.currentTarget.checked)}
              />
            ) : null}
          </div>

          <div className="field schema-field">
            <FieldLabel htmlFor={`mixer-${kind}-notes`} help="mixer.channel.notes">
              Notes
            </FieldLabel>
            <textarea
              id={`mixer-${kind}-notes`}
              className="input"
              rows={2}
              value={notes}
              onChange={(event) => setNotes(event.currentTarget.value)}
            />
          </div>

          <div className="field schema-field">
            <FieldLabel htmlFor={`mixer-${kind}-order`} help="mixer.channel.order">
              Order
            </FieldLabel>
            <Input
              id={`mixer-${kind}-order`}
              type="number"
              mono
              value={sortOrder}
              onChange={(event) => setSortOrder(Number(event.currentTarget.value))}
            />
          </div>

          {localError ? (
            <p className="field-note" role="alert">
              {localError}
            </p>
          ) : null}
          {errors["name"] ? <p className="field-note">{errors["name"]}</p> : null}
          {errors["driver_refs"] ? <p className="field-note">{errors["driver_refs"]}</p> : null}
          {errors["short_name"] ? <p className="field-note">{errors["short_name"]}</p> : null}

          <div className="dialog-actions">
            <Button variant="secondary" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" variant="primary" helpId="mixer.channel.save" loading={saving}>
              {channel ? "Save" : `Add ${noun}`}
            </Button>
          </div>
        </form>
      </SheetContent>

      <ConflictDialog
        open={conflict !== null}
        onOpenChange={(next) => {
          if (!next) onConflictDismiss();
        }}
        deviceName={channel ? `"${channel.name}"` : `This ${noun}`}
        rows={conflict ? diffRecord(conflict.current as unknown as Record<string, unknown>, buildBody() as unknown as Record<string, unknown>) : ([] as ConflictRow[])}
        onReload={onConflictReload}
        onOverwrite={() => submit(conflict?.current.updated_at)}
      />
    </Sheet>
  );
}
