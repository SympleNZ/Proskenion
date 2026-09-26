/*
 * The multi-select **Set level** action (spec §21.12): one slider, applied
 * proportionally across the selection, sent as a single `POST
 * .../lighting/levels` using the Lighting view's shared Fade time (§21.11,
 * §21.12) — so every selected fixture's fade starts together as one intent.
 *
 * "Proportionally" (§21.12 leaves the meaning undefined): the target is
 * where the BRIGHTEST selected fixture ends up; every other selected
 * fixture scales by the same ratio, preserving the selection's relative
 * balance — see `proportionalLevel.ts` for the full reasoning. This help
 * text says so directly, since the control's effect is not obvious from a
 * single slider alone.
 *
 * Under external control (§21.11) the caller passes only the fixtures that
 * stay live, the KNX house dimmers, and says how many stage fixtures it left
 * out (`skipped`); the sheet says how many, so a partial Set level is
 * never silent. With nothing left to set, Apply is disabled and the sheet says why.
 */
import { useId, useState } from "react";
import { Zap } from "lucide-react";

import { Button } from "@/components/ui/Button";
import { Sheet, SheetContent } from "@/components/ui/Sheet";
import { useFadeSeconds } from "@/lighting/fadeTime";

export interface SetLevelSheetProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** How many selected fixtures this Set level applies to. */
  count: number;
  /** Selected stage (DMX) fixtures left out because external control is active. */
  skipped?: number;
  /** The brightest currently-selected fixture's displayed level, the slider's starting point. */
  initialLevel: number;
  applying: boolean;
  onApply: (targetLevel: number) => void;
}

/**
 * The caller remounts this component (a fresh `key`) each time it opens, so
 * `initialLevel` — computed from whatever is selected right then — seeds
 * `level` correctly every time; Radix's own `onOpenChange` fires only for
 * Radix-initiated closes (Escape, outside click), never for this externally
 * controlled `open` prop, so resetting there would miss every open.
 */
export function SetLevelSheet({ open, onOpenChange, count, skipped = 0, initialLevel, applying, onApply }: SetLevelSheetProps) {
  const [level, setLevel] = useState(initialLevel);
  const fadeSeconds = useFadeSeconds();
  const sliderId = useId();

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent
        title={`Set level — ${count + skipped} selected`}
        description={`Applies proportionally: the brightest of the ${count} ${skipped > 0 ? "house dimmers being set" : "selected fixtures"} moves to this level, and the rest scale by the same ratio, keeping their relative balance. Fades over ${fadeSeconds.toFixed(1)}s — the Lighting view's Fade time.`}
      >
        {skipped > 0 ? (
          <p className="lighting-lockout" role="status">
            <Zap aria-hidden="true" className="size-4" />
            {count > 0
              ? `External control is active: ${plural(skipped, "stage fixture")} skipped. Only the ${plural(count, "house dimmer")} ${count === 1 ? "is" : "are"} set.`
              : "External control is active: stage fixtures are read-only, and nothing selected can be set."}
          </p>
        ) : null}
        <div className="field">
          <label className="field-label" htmlFor={sliderId}>
            Level
          </label>
          <input
            id={sliderId}
            type="range"
            min={0}
            max={100}
            step={1}
            value={level}
            onChange={(event) => setLevel(Number(event.target.value))}
          />
          <span className="lighting-fade-value">{level.toFixed(0)}%</span>
        </div>
        <div className="dialog-actions">
          <Button variant="secondary" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button variant="primary" loading={applying} disabled={count === 0} onClick={() => onApply(level)}>
            Apply
          </Button>
        </div>
      </SheetContent>
    </Sheet>
  );
}

function plural(n: number, noun: string): string {
  return `${n} ${noun}${n === 1 ? "" : "s"}`;
}
