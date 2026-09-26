/*
 * A client-side mirror of `proskenion/rules/model.py`'s DPT classing and
 * `proskenion/core/knx_dpt.py`'s §7.1 table, so a match type the server would
 * refuse is disabled here for the same reason, with the reason shown, before
 * the request is ever sent (spec §21.17: "Match types are disabled according
 * to the chosen address's data type, with the reason"). The server's own
 * `422` is still the last word — see `RuleEditor.tsx` — this only saves a
 * round trip for the common case.
 *
 * There is no shared source for this table between the backend and the
 * frontend, so a DPT added to `knx_dpt.py` needs this file updated too; that
 * duplication is called out in the task report rather than silently risking
 * drift.
 */
import type { MatchType } from "./types";

export type DptClass = "boolean" | "numeric" | "other";

const BOOLEAN_MATCH_TYPES: ReadonlySet<MatchType> = new Set(["any", "equal", "not_equal"]);
const ALL_MATCH_TYPES: ReadonlySet<MatchType> = new Set(["any", "equal", "not_equal", "gte", "lte", "range"]);
const OTHER_MATCH_TYPES: ReadonlySet<MatchType> = new Set(["any"]);

/** The class of a registered DPT, or `null` if §7.1's table has no codec for it. */
export function dptClass(dpt: string): DptClass | null {
  const match = /^(\d+)(?:\.(\d+))?$/.exec(dpt.trim());
  if (!match) return null;
  const main = Number(match[1]);
  const sub = match[2] === undefined ? null : Number(match[2]);
  if (main === 1) return "boolean"; // every 1.x type, including 1.001 and 1.008 (§7.1)
  if (main === 3) return sub === 7 ? "other" : null;
  if (main === 5) return sub === 1 || sub === 10 ? "numeric" : null;
  if (main === 9) return "numeric";
  if (main === 20) return "numeric";
  return null;
}

export function allowedMatchTypes(cls: DptClass): ReadonlySet<MatchType> {
  if (cls === "boolean") return BOOLEAN_MATCH_TYPES;
  if (cls === "numeric") return ALL_MATCH_TYPES;
  return OTHER_MATCH_TYPES;
}

/** Why a match type is disabled for this DPT, shown beside the picker (§21.17, §8.4). */
export function matchTypeDisabledReason(matchType: MatchType, dpt: string): string | null {
  const cls = dptClass(dpt);
  if (cls === null) {
    return `DPT ${dpt} is not supported (§7.1); its telegrams never reach the rule layer`;
  }
  if (allowedMatchTypes(cls).has(matchType)) return null;
  if (cls === "boolean") return "gte, lte and range are numeric only — a 1-bit address is only ever 0 or 1 (§8.4)";
  return "3.007 (dimming control) carries a direction and a step count, not a comparable value — only “any” is offered";
}

export const MATCH_TYPE_LABELS: Readonly<Record<MatchType, string>> = {
  any: "Any value",
  equal: "Equal to",
  not_equal: "Not equal to",
  gte: "At least (≥)",
  lte: "At most (≤)",
  range: "Between (range)",
};
