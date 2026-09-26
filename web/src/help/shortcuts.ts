/*
 * The keyboard shortcut reference (spec §24.2), listed here once so the `?`
 * sheet and this file are the only place the list is maintained.
 *
 * Every row is checked against what actually runs, not transcribed from the
 * spec — `implemented: false` means grepping the source found no matching
 * handler when this file was written; `note` records anything narrower than
 * §24.2's plain description. A row never claims more than the code does.
 *
 * `tiers`, when given, is which of the three tiers actually have the surface
 * a row describes — admin's own forms and the scene editor do not exist in
 * the operator or hirer shells, so listing them there would describe a key
 * that does nothing. Omitted means all three (spec §21.24: a hirer's `?`
 * sheet still gets "their shortcuts" — the ones that apply to them).
 */
import type { Tier } from "@/api/auth";

export interface Shortcut {
  keys: string;
  description: string;
  implemented: boolean;
  note?: string;
  tiers?: readonly Tier[];
}

export const SHORTCUTS: readonly Shortcut[] = [
  { keys: "Escape", description: "Close the open modal, sheet or popover", implemented: true },
  { keys: "Enter", description: "Confirm a dialog, or submit the focused form", implemented: true },
  { keys: "Ctrl/Cmd+S", description: "Save the current form", implemented: true, tiers: ["admin"] },
  { keys: "?", description: "Open this keyboard reference", implemented: true },
  { keys: "Tab", description: "Focus a fader", implemented: true },
  { keys: "Arrow Up / Down", description: "Fader ±1 dB (mixer) or ±1% (lighting)", implemented: true },
  { keys: "Page Up / Down", description: "Fader ±5 dB (mixer) or ±10% (lighting)", implemented: true },
  { keys: "Home", description: "Fader to minimum", implemented: true },
  { keys: "End", description: "Fader to maximum", implemented: true },
  {
    keys: "Arrow Up / Down",
    description: "Reorder the focused action card in the scene editor",
    implemented: true,
    note: "The Move earlier/Move later buttons still work too; both announce the card's new position.",
    tiers: ["admin"],
  },
] as const;

export function shortcutsForTier(tier: Tier): readonly Shortcut[] {
  return SHORTCUTS.filter((shortcut) => !shortcut.tiers || shortcut.tiers.includes(tier));
}
