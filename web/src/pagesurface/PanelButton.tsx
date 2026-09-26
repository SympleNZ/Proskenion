/*
 * One panel button (spec §21.9 "Each button fires a rule and takes its lamp
 * from a derived status" / "Two indications, doing different jobs"). Two
 * indications carry the state — an LED bar anchored to the top (the precise
 * indicator, trusted standing at the tablet) and a border glow (what reads
 * from across the auditorium) — and the button face itself stays dark in
 * both states: filling it with colour would turn a panel of active buttons
 * into coloured rectangles.
 *
 * The lamp, never the fire response, decides the lit state (`@/live/store`'s
 * `status` frame) — a button with no `state_id` never latches. Firing
 * only starts the rule; §21.27's success/failure treatment (mirrored from
 * `admin/rules/RulesTab.tsx`'s own `fireOutcomeToast`) reports whether it
 * actually ran.
 */
import { useRef, useState, type CSSProperties, type PointerEvent as ReactPointerEvent } from "react";
import { toast } from "sonner";

import { presentError } from "@/api/errors";
import { ConfirmDialog } from "@/components/ui/Sheet";
import { useLamp } from "@/live/store";

import { hirerButtonOutcomeToast, panelButtonOutcomeToast, useFireButton } from "./api";
import type { PanelButtonSpec } from "./types";

/** A pointer past this many client px before release is a scroll, not a tap — mirrors `stageplan/FixtureNode.tsx`'s own `TAP_MOVE_THRESHOLD` (CONVENTIONS "a scroll is not a tap"). */
const TAP_MOVE_THRESHOLD = 8;

/** The group palette's closed set of names (§21.3) — anything else falls back to the accent colour rather than reaching an arbitrary string into a CSS custom property. */
const GROUP_COLOUR_NAMES = new Set([
  "rose",
  "salmon",
  "tangerine",
  "amber",
  "lime",
  "fern",
  "ocean",
  "azure",
  "violet",
  "orchid",
  "silver",
  "white",
]);

function accentVar(colour: string | null): string {
  return colour && GROUP_COLOUR_NAMES.has(colour) ? `var(--group-${colour})` : "var(--color-teal-500)";
}

/** Three sizes, matching `docs/pages-device-sizes.html`'s own button label sizing, expressed as design tokens rather than the mock's raw px. */
function labelFontSize(side: number): string {
  if (side < 90) return "var(--text-xs)";
  if (side < 120) return "var(--text-sm)";
  return "var(--text-base)";
}

export interface PanelButtonProps {
  pageId: number;
  spec: PanelButtonSpec;
  /** The button's square side, in px, from the panel's own size/column search (§21.9). */
  side: number;
  /** Plain "Done"/failure notifications rather than the operator's own guard/result wording (§21.15, §24.6). */
  hirer?: boolean;
}

export function PanelButton({ pageId, spec, side, hirer = false }: PanelButtonProps) {
  const rawLamp = useLamp(spec.state_id ?? -1);
  const lamp = spec.state_id !== null ? rawLamp : null;
  const transitioning = lamp?.transitioning === true;
  const on = !transitioning && lamp?.on === true;
  const lampState: "on" | "transitioning" | "off" = transitioning ? "transitioning" : on ? "on" : "off";

  const [confirmOpen, setConfirmOpen] = useState(false);
  const fireButton = useFireButton();

  const pointerStart = useRef<{ x: number; y: number } | null>(null);
  const moved = useRef(false);

  function handlePointerDown(event: ReactPointerEvent<HTMLButtonElement>): void {
    pointerStart.current = { x: event.clientX, y: event.clientY };
    moved.current = false;
  }

  function handlePointerMove(event: ReactPointerEvent<HTMLButtonElement>): void {
    const start = pointerStart.current;
    if (!start) return;
    const dx = event.clientX - start.x;
    const dy = event.clientY - start.y;
    if (Math.hypot(dx, dy) > TAP_MOVE_THRESHOLD) moved.current = true;
  }

  function handlePointerEnd(): void {
    pointerStart.current = null;
  }

  function fire(): void {
    fireButton.mutate(
      { pageId, buttonId: spec.id },
      {
        onSuccess: (response) => (hirer ? hirerButtonOutcomeToast(response) : panelButtonOutcomeToast(response, spec.label)),
        onError: (error) => {
          if (hirer) toast.error("Something went wrong — please speak to venue staff");
          else void presentError(error);
        },
      },
    );
  }

  function handleClick(): void {
    // A scroll across the surface is never a tap (CONVENTIONS): a pointer
    // that moved past the threshold before release fires nothing.
    if (moved.current) {
      moved.current = false;
      return;
    }
    if (spec.confirm) {
      setConfirmOpen(true);
      return;
    }
    fire();
  }

  const style = {
    "--panel-button-accent": accentVar(spec.colour),
    "--panel-button-font-size": labelFontSize(side),
  } as CSSProperties;

  return (
    <>
      <button
        type="button"
        className="panel-button"
        data-lamp={lampState}
        style={style}
        disabled={fireButton.isPending}
        aria-label={`${spec.label}, ${lampState}`}
        onPointerDown={handlePointerDown}
        onPointerMove={handlePointerMove}
        onPointerUp={handlePointerEnd}
        onPointerCancel={handlePointerEnd}
        onClick={handleClick}
        data-testid={`panel-button-${spec.id}`}
      >
        <span className="panel-button-led" aria-hidden="true" />
        <span className="panel-button-label">{spec.label}</span>
      </button>
      {spec.confirm ? (
        <ConfirmDialog
          open={confirmOpen}
          onOpenChange={setConfirmOpen}
          title={spec.label}
          description={`This fires "${spec.label}" now.`}
          confirmLabel="Fire"
          onConfirm={() => {
            setConfirmOpen(false);
            fire();
          }}
        />
      ) : null}
    </>
  );
}
