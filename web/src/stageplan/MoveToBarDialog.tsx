/*
 * "Move to another bar" (spec §21.18's long-press menu) — distinct from
 * dragging: a bar picker rather than a drop point, for when the target bar
 * is off-screen or the fixture's on-bar position does not need to change.
 */
import { Dialog } from "radix-ui";

import { Button } from "@/components/ui/Button";

import type { LightingBar } from "./types";

export interface MoveToBarDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  fixtureName: string;
  bars: readonly LightingBar[];
  currentBarId: number | null;
  moving: boolean;
  onMove: (barId: number) => void;
}

export function MoveToBarDialog({ open, onOpenChange, fixtureName, bars, currentBarId, moving, onMove }: MoveToBarDialogProps) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="dialog-backdrop" />
        <Dialog.Content className="dialog-content" aria-labelledby="move-to-bar-title">
          <Dialog.Title className="sheet-title" id="move-to-bar-title">
            Move {fixtureName} to another bar
          </Dialog.Title>
          <Dialog.Description className="text-fg-secondary">Its on-bar position stays the same.</Dialog.Description>
          <ul className="flex flex-col gap-1" role="list">
            {bars.map((bar) => (
              <li key={bar.id}>
                <Button
                  variant={bar.id === currentBarId ? "primary" : "secondary"}
                  block
                  disabled={moving || bar.id === currentBarId}
                  onClick={() => onMove(bar.id)}
                >
                  {bar.name}
                  {bar.id === currentBarId ? " (current)" : ""}
                </Button>
              </li>
            ))}
          </ul>
          <div className="dialog-actions">
            <Button variant="secondary" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
