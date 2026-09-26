/*
 * Q17's four conditions, and which of them is blocking right now (§21.24:
 * "why hasn't it applied yet" has to be answerable from the screen). Shown
 * both while choosing how to apply — so the choice is informed — and while
 * an update is armed and waiting, where it is the whole of what the screen
 * has to say.
 */
import { Check, X } from "lucide-react";

import { QUIET_CONDITION_KEYS, QUIET_CONDITION_LABELS, type QuietReport } from "./types";

export interface QuietConditionsProps {
  quiet: QuietReport;
}

export function QuietConditions({ quiet }: QuietConditionsProps) {
  return (
    <ul className="flex flex-col gap-1" aria-label="The next quiet moment's conditions">
      {QUIET_CONDITION_KEYS.map((key) => {
        const met = quiet[key];
        return (
          <li key={key} className="condition-row" data-met={met}>
            {met ? (
              <Check aria-hidden="true" strokeWidth={3} className="size-4 text-success-text" />
            ) : (
              <X aria-hidden="true" strokeWidth={3} className="size-4 text-danger-text" />
            )}
            <span>{QUIET_CONDITION_LABELS[key]}</span>
            {!met ? (
              <span className="text-fg-muted text-xs" aria-hidden="true">
                — blocking
              </span>
            ) : null}
          </li>
        );
      })}
    </ul>
  );
}
