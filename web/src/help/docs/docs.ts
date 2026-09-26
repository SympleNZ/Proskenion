/*
 * The documentation bundled into the web build and rendered inside the app
 * (spec §21.24 *Help*'s "link to the online documentation" — Simon decided,
 * 26 Sep 2026, to serve it from the controller itself instead: the repository
 * is private and the appliance may be offline, so an actual online link has
 * nowhere to go). Each file is pulled in with Vite's `?raw` import, so its
 * exact text ships inside the bundle and always matches the installed
 * version — there is no separate fetch, and it works with no internet.
 *
 * These are the only docs bundled — not the whole `docs/` tree — chosen
 * because they are what a person standing at the rack or the touch screen
 * actually needs while the appliance is in front of them. `recovery.md`,
 * `hardware/setup.md` and the rest stay repository-only reference material.
 */
import accessibilityCheckRaw from "../../../../docs/hardware/accessibility-check.md?raw";
import hireHandoverRaw from "../../../../docs/run-sheets/hire-handover.md?raw";
import operatorQuickReferenceRaw from "../../../../docs/run-sheets/operator-quick-reference.md?raw";
import recoveryCardRaw from "../../../../docs/run-sheets/recovery-card.md?raw";
import type { Tier } from "@/api/auth";

export type DocId = "operator-quick-reference" | "hire-handover" | "recovery-card" | "accessibility-check";

export interface DocEntry {
  id: DocId;
  /** The bare filename, for matching a Markdown link's target (`markdown.tsx`). */
  filename: string;
  title: string;
  /** Which tiers see this doc listed in the `?` sheet's Documentation section. */
  tiers: readonly Tier[];
  raw: string;
}

export const DOCS: readonly DocEntry[] = [
  {
    id: "operator-quick-reference",
    filename: "operator-quick-reference.md",
    title: "Operator quick reference",
    tiers: ["operator", "admin"],
    raw: operatorQuickReferenceRaw,
  },
  {
    id: "hire-handover",
    filename: "hire-handover.md",
    title: "Hire handover",
    tiers: ["admin"],
    raw: hireHandoverRaw,
  },
  {
    id: "recovery-card",
    filename: "recovery-card.md",
    title: "Recovery card",
    tiers: ["admin"],
    raw: recoveryCardRaw,
  },
  {
    id: "accessibility-check",
    filename: "accessibility-check.md",
    title: "Accessibility check",
    tiers: ["admin"],
    raw: accessibilityCheckRaw,
  },
];

export function docsForTier(tier: Tier): readonly DocEntry[] {
  return DOCS.filter((doc) => doc.tiers.includes(tier));
}

export function getDoc(id: DocId): DocEntry {
  const doc = DOCS.find((d) => d.id === id);
  if (!doc) throw new Error(`docs.ts: no bundled doc with id ${id}`);
  return doc;
}
