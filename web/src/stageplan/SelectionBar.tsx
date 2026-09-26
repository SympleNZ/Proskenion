/*
 * The multi-select action bar (spec §21.12): "N selected [Group] [Set
 * level] [✕]", animated up from the bottom on first selection
 * (`.stage-plan-selection-bar`'s `slide-up` animation in components.css).
 * Admin only — an operator never multi-selects (§21.12's capability table),
 * so this never mounts for one.
 *
 * `setLevelUnavailable` disables Set level and shows why, beside the button
 * and as its description: a selection of stage fixtures only, while external
 * control is active (§21.11, `setLevelTargetsUnderExternalControl`).
 */
import { useId } from "react";
import { X, Zap } from "lucide-react";

import { Button } from "@/components/ui/Button";

export interface SelectionBarProps {
  count: number;
  onGroup: () => void;
  onSetLevel: () => void;
  onClear: () => void;
  /** Why Set level cannot be used right now, if it cannot; the button is then disabled. */
  setLevelUnavailable?: string | undefined;
}

export function SelectionBar({ count, onGroup, onSetLevel, onClear, setLevelUnavailable }: SelectionBarProps) {
  const reasonId = useId();
  if (count === 0) return null;
  return (
    <div className="stage-plan-selection-bar" role="toolbar" aria-label="Selected fixtures">
      <span className="stage-plan-selection-count">
        {count} selected
      </span>
      <Button variant="secondary" onClick={onGroup}>
        Group
      </Button>
      {setLevelUnavailable ? (
        <span className="lighting-lockout" id={reasonId}>
          <Zap aria-hidden="true" className="size-4" />
          {setLevelUnavailable}
        </span>
      ) : null}
      <Button
        variant="secondary"
        onClick={onSetLevel}
        disabled={setLevelUnavailable !== undefined}
        aria-describedby={setLevelUnavailable ? reasonId : undefined}
      >
        Set level
      </Button>
      <Button variant="ghost" size="icon" aria-label="Clear selection" onClick={onClear}>
        <X aria-hidden="true" className="size-5" />
      </Button>
    </div>
  );
}
