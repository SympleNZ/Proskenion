/*
 * The inline help affordance (spec §19.1, §21.24 *Help*): a small "i" button
 * next to a control's label that opens its help text in a popover. Built on
 * Radix Popover (already a dependency via the `radix-ui` package used for
 * `Sheet`/`ConfirmDialog` and `Menu`, §21 — no new package). Works with a
 * pointer, with the keyboard (the trigger is a real `<button>`, Enter/Space
 * opens it, Escape closes it and returns focus) and with a screen reader
 * (`aria-label` names what the popover explains; Radix wires the
 * `aria-expanded`/`aria-haspopup` state on the trigger itself).
 *
 * `data-help-trigger` marks a rendered instance so `coverage.ts` can find it
 * without depending on any particular DOM shape.
 */
import { Info } from "lucide-react";
import { Popover } from "radix-ui";
import type { ReactNode } from "react";

import { getHelp, type HelpId } from "./registry";

export interface HelpButtonProps {
  id: HelpId;
  /** Rare: a control whose visible label does not already say what this is help for. */
  label?: string | undefined;
}

export function HelpButton({ id, label }: HelpButtonProps) {
  const entry = getHelp(id);
  return (
    <Popover.Root>
      <Popover.Trigger asChild>
        <button
          type="button"
          className="btn btn-ghost btn-icon help-trigger"
          data-help-trigger
          data-help-id={id}
          aria-label={`Help: ${label ?? entry.term}`}
        >
          <Info aria-hidden="true" className="size-4" />
        </button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content className="help-popover" sideOffset={8} collisionPadding={8}>
          <p className="help-popover-term">{entry.term}</p>
          <p className="help-popover-body">{entry.body}</p>
          <Popover.Arrow className="help-popover-arrow" />
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}

export interface FieldLabelProps {
  htmlFor: string;
  help?: HelpId | undefined;
  required?: boolean | undefined;
  children: ReactNode;
}

/**
 * A `.field-label` plus its optional help button, for the many admin forms
 * that build a `.field` by hand rather than through `ui/Input`'s `Field`
 * (whose fields are driver-declared and carry their own per-field help —
 * see `coverage.ts`).
 */
export function FieldLabel({ htmlFor, help, required, children }: FieldLabelProps) {
  return (
    <div className="field-label-row">
      <label className="field-label" htmlFor={htmlFor}>
        {children}
        {required ? (
          <span className="field-required" aria-hidden="true">
            {" "}
            (required)
          </span>
        ) : null}
      </label>
      {help ? <HelpButton id={help} /> : null}
    </div>
  );
}
