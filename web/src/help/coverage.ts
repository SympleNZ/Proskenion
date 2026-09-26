/*
 * The help coverage check (spec §19.1 "Inline help is written for every new
 * admin control"). Run against a rendered admin screen, it reports every
 * control that has no help affordance — see `coverage.test.tsx` for the
 * screen-by-screen tests that call this, including the negative cases that
 * prove it actually catches a missing one.
 *
 * "Admin control" is defined mechanically, over the rendered DOM rather than
 * the source, so a control that is conditionally rendered is only checked
 * once it is actually on screen:
 *
 *  - every `.field` that is a labelled form field, EXCEPT one carrying
 *    `data-field` — the marker `SchemaForm.tsx` puts on a field it generated
 *    from a driver's own declared schema (spec §5.5). Those fields are not
 *    known ahead of time (a driver that ships later renders one with no
 *    interface change) and already carry the driver's own per-field
 *    `field.help` text, so they are a different, already-satisfied kind of
 *    inline help rather than one this registry can pre-write;
 *  - every primary action button (`.btn-primary` — Save, Add, and the like;
 *    a secondary Cancel or Close is not required to explain itself);
 *  - every destructive button — `.btn-destructive` (Delete, Remove, Roll
 *    back, and the like, wherever the button itself carries the variant),
 *    and, since a destructive action is often gated behind a plain secondary
 *    button that opens a `ConfirmDialog` rather than styled destructive
 *    itself (the 26 Sep milestone audit's example: Restart, Restore and
 *    Reboot on the Backup and Updates screens), any button marked
 *    `confirmTrigger` (`ui/Button`'s prop, `[data-confirm-trigger]` in the
 *    DOM). A `ConfirmDialog` itself is a controlled Radix dialog that is not
 *    in the DOM at all until it is open, so the trigger that opens it is the
 *    only thing a resting-state sweep can check — the dialog's own confirm
 *    button is out of scope, the same way a `Sheet`'s Save button already is.
 *
 * A control is "covered" if it has a `[data-help-trigger]` — a `HelpButton`
 * — of its own (for a `.field`; usually inside the `.field-label-row`
 * `FieldLabel` renders, but a `<fieldset>` whose label is a `<legend>` puts
 * one directly beside it instead) or as its wrapping `.btn-help-group`
 * sibling (for a button; `ui/Button`'s own `helpId` prop renders that
 * wrapper). A field's own help is looked up among its *direct* children
 * only, skipping any that are themselves a nested `.field` — a `.field`
 * commonly contains other `.field`s (a profile's per-channel role and
 * default, say), and searching the whole subtree would let an inner
 * field's help cover the outer one's different label by accident.
 *
 * Separately, and coarser: every `.card` (`ui/Card`) that contains a control
 * of one of the kinds above, *or* a bare `input`/`select`/`textarea` not
 * wrapped in a `.field` (`ui/Select`'s `Checkbox` renders its `<input>`
 * straight into a `<label>`, with no `.field` around it) must contain at
 * least one help trigger *somewhere* in it, not necessarily on the exact
 * control that needs it. This is the net the audit's five never-rendered
 * cards (Snapshots, Images, the OS section, Restart/Reboot, Debug logging)
 * fell through: none of their controls were a `.field` or a `.btn-primary`,
 * so a card that opted into no coverage test at all was invisible twice
 * over. A card with a per-field or per-button help trigger already
 * satisfies this the same instant it satisfies the finer-grained rule above
 * — this only bites a card that has *no* help anywhere, such as a checkbox
 * list with a card-level explanation instead of one per row.
 *
 * Deliberately narrower than "any button": a plain secondary button that
 * only opens another already-covered surface — an "Edit" row opening a
 * sheet full of its own labelled fields, or a disclosure toggle expanding a
 * panel — is chrome, not a control whose own meaning needs explaining, the
 * same way a secondary Cancel is exempt from the per-button rule above. A
 * card consisting only of such a button — a bare list row with an Edit
 * button and nothing else — is not required to carry its own help trigger.
 * Likewise `input[type="file"]` is excluded: every one of them in this
 * codebase (`RestoreCard`, `UpdateSection`, `ImportWizard`) is `sr-only`,
 * opened by a visible button that already explains itself — the file input
 * is plumbing for the native picker, not a second control alongside it.
 */
export interface MissingControl {
  kind: "field" | "button" | "card";
  /** The label, button text or card title, for a readable test failure. */
  description: string;
}

function describe(el: Element): string {
  const text = el.textContent?.replace(/\s+/g, " ").trim();
  return text && text.length > 0 ? text : "(unlabelled)";
}

function hasOwnHelp(field: Element): boolean {
  return Array.from(field.children).some((child) => {
    if (child.classList.contains("field")) return false; // belongs to a nested field, not this one
    return child.matches("[data-help-trigger]") || child.querySelector("[data-help-trigger]") !== null;
  });
}

export function findMissingHelp(container: ParentNode): MissingControl[] {
  const missing: MissingControl[] = [];

  container.querySelectorAll(".field").forEach((field) => {
    if (field.hasAttribute("data-field")) return;
    if (hasOwnHelp(field)) return;
    const label = field.querySelector(".field-label");
    missing.push({ kind: "field", description: label ? describe(label) : describe(field) });
  });

  container.querySelectorAll(".btn-primary, .btn-destructive, [data-confirm-trigger]").forEach((button) => {
    if (button.matches("[data-help-trigger]")) return; // not expected, but never self-flag
    const wrapper = button.closest(".btn-help-group");
    if (wrapper?.querySelector("[data-help-trigger]")) return;
    missing.push({ kind: "button", description: describe(button) });
  });

  container.querySelectorAll(".card").forEach((card) => {
    if (card.querySelector(".btn-primary, .btn-destructive, [data-confirm-trigger], input:not([type=file]), select, textarea") === null) return;
    if (card.querySelector("[data-help-trigger]") !== null) return;
    const title = card.querySelector(".card-title");
    missing.push({ kind: "card", description: title ? describe(title) : describe(card) });
  });

  return missing;
}

/** A one-line summary for a failed assertion — every missing control, not just the count. */
export function describeMissing(missing: readonly MissingControl[]): string {
  return missing.map((m) => `${m.kind}: ${m.description}`).join("\n");
}
