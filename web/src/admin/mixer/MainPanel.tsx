/*
 * Admin → Mixer → Main and outputs → the Main panel (§21.21, §7.3): "A Main
 * channel is always present... The admin can rename it and optionally expose
 * it to hirers... Once created it cannot be deleted." Its `channel_kind`
 * never changes either, so this panel edits only what §21.21 draws for it —
 * name, staff visibility, the hirer ceiling and tracking — and offers no
 * reference picker, because Main's own reference is fixed by the driver at
 * creation (§7.3).
 *
 * A Delete action is still offered rather than omitted: the contract answers
 * it with `validation_failed`, `detail.reason = "main_immutable"` (§21.21),
 * and that refusal has to be something a real admin can trigger and see, not
 * just a fact recorded in a comment.
 */
import { useState } from "react";

import { ApiError } from "@/api/client";
import { presentError } from "@/api/errors";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { Input } from "@/components/ui/Input";
import { Checkbox } from "@/components/ui/Select";
import { ConfirmDialog } from "@/components/ui/Sheet";
import { FieldLabel } from "@/help/HelpButton";
import type { FaderLawPoint } from "@/lib/faderLaw";

import { useDeleteChannel, useUpdateChannel } from "./api";
import { HirerCeilingControl } from "./HirerCeilingControl";
import type { MixerChannel } from "./types";

export interface MainPanelProps {
  channel: MixerChannel;
  faderLaw: readonly FaderLawPoint[];
}

export function MainPanel({ channel, faderLaw }: MainPanelProps) {
  const [name, setName] = useState(channel.name);
  const [visibleStaff, setVisibleStaff] = useState(channel.visible_staff);
  const [hirerMaxDb, setHirerMaxDb] = useState<number | null>(channel.hirer_max_db);
  const [tracked, setTracked] = useState(channel.tracked);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [deleteRefusal, setDeleteRefusal] = useState<string | null>(null);

  const update = useUpdateChannel();
  const remove = useDeleteChannel();

  const dirty =
    name.trim() !== channel.name || visibleStaff !== channel.visible_staff || hirerMaxDb !== channel.hirer_max_db || tracked !== channel.tracked;

  function save(): void {
    update.mutate(
      {
        id: channel.id,
        version: channel.updated_at,
        body: { name: name.trim(), visible_staff: visibleStaff, hirer_max_db: hirerMaxDb, tracked },
      },
      { onError: (error) => presentError(error) },
    );
  }

  function requestDelete(): void {
    setDeleteRefusal(null);
    remove.mutate(channel.id, {
      onError: (error) => {
        if (error instanceof ApiError && error.code === "validation_failed" && error.reason === "main_immutable") {
          setDeleteRefusal("Main cannot be deleted — it can be renamed, but a mixer always keeps a Main output (§7.3).");
          return;
        }
        presentError(error);
      },
    });
  }

  return (
    <section className="device-group" aria-labelledby="mixer-main-heading">
      <h2 className="section-title" id="mixer-main-heading">
        Main LR output
      </h2>
      <Card className="device-card" compact>
        <div className="field schema-field">
          <FieldLabel htmlFor="mixer-main-name" help="mixer.main.name">
            Name
          </FieldLabel>
          <Input id="mixer-main-name" value={name} onChange={(event) => setName(event.currentTarget.value)} />
        </div>
        <div className="flex flex-wrap gap-4">
          <Checkbox
            id="mixer-main-staff"
            label="Visible to staff"
            checked={visibleStaff}
            onChange={(event) => setVisibleStaff(event.currentTarget.checked)}
          />
          <Checkbox id="mixer-main-tracked" label="Track state" checked={tracked} onChange={(event) => setTracked(event.currentTarget.checked)} />
        </div>
        <HirerCeilingControl id="mixer-main-hirer-max" value={hirerMaxDb} onChange={setHirerMaxDb} law={faderLaw} />
        {deleteRefusal ? (
          <p className="field-note" role="alert">
            {deleteRefusal}
          </p>
        ) : null}
        <div className="dialog-actions">
          <Button variant="destructive" helpId="mixer.main.delete" onClick={() => setConfirmingDelete(true)} loading={remove.isPending}>
            Delete
          </Button>
          <Button variant="primary" helpId="mixer.main.save" onClick={save} loading={update.isPending} disabled={!dirty || name.trim().length === 0}>
            Save
          </Button>
        </div>
      </Card>

      <ConfirmDialog
        open={confirmingDelete}
        onOpenChange={setConfirmingDelete}
        title={`Delete "${channel.name}"?`}
        description="Main cannot actually be removed — this exists to show what the controller says when you try (§21.21)."
        confirmLabel="Delete"
        destructive
        onConfirm={() => {
          setConfirmingDelete(false);
          requestDelete();
        }}
      />
    </section>
  );
}
