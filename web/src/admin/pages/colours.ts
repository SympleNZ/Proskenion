/*
 * The group palette (§21.3, §15.12 "group palette token"). A page button's
 * `colour` is one of these token names, never a raw hex value — the closed
 * set matches `tokens.css`'s `--group-*` custom properties exactly, and the
 * swatch reads its colour from the CSS variable rather than a literal
 * (`discipline.test.ts` bans a raw colour anywhere outside `tokens.css`).
 */
import type { GroupColourToken } from "./types";

export interface ColourSwatch {
  token: GroupColourToken;
  label: string;
}

/** In palette order, matching `tokens.css` and the admin mock-up's swatch row. */
export const GROUP_COLOURS: readonly ColourSwatch[] = [
  { token: "rose", label: "Rose" },
  { token: "salmon", label: "Salmon" },
  { token: "tangerine", label: "Tangerine" },
  { token: "amber", label: "Amber" },
  { token: "lime", label: "Lime" },
  { token: "fern", label: "Fern" },
  { token: "ocean", label: "Ocean" },
  { token: "azure", label: "Azure" },
  { token: "violet", label: "Violet" },
  { token: "orchid", label: "Orchid" },
  { token: "silver", label: "Silver" },
  { token: "white", label: "White" },
];

/** `var(--group-<token>)` — a CSS variable reference, never a raw colour (`discipline.test.ts`). */
export function groupColourVar(token: GroupColourToken): string {
  return `var(--group-${token})`;
}
