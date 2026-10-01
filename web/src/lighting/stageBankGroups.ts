/*
 * Which `lighting_group` rules make one stage bank (owner decision
 * 2026-09-30). The rig's wall panel has one "all" switch (`4/0/8`) that
 * drives four rules — "Stage all → row 1" … "→ row 4", one per row group,
 * since a binding cannot target the indicator-only "Stage all" group. One
 * switch on the wall is one button here: rules are grouped by the KNX
 * address that triggers them.
 *
 * - The label is the rules' shared name prefix before " → " when every rule
 *   on the address has one ("Stage all"), else the first rule's name. (The
 *   address's own name is admin-only — `GET /knx/addresses` — so the
 *   operator's view cannot use it.)
 * - A press fires every enabled rule on the address with the same value.
 * - The lamp is on only while every rule's binding reports on.
 *
 * A rule with no KNX trigger is a bank of its own, as is a rule alone on its
 * address — both render exactly as before.
 */
import type { StageBankRule } from "./types";

export interface StageBank {
  /** Stable across renders: the address, or the lone rule. */
  key: string;
  label: string;
  /** Every rule on the address, in the order they were listed (the rules' run order). */
  rules: readonly StageBankRule[];
  /** The rules a press fires: the enabled ones — or all of them, if none is enabled. */
  fires: readonly StageBankRule[];
}

const SEPARATOR = " → ";

function sharedPrefix(rules: readonly StageBankRule[]): string | null {
  let prefix: string | null = null;
  for (const rule of rules) {
    const at = rule.name.indexOf(SEPARATOR);
    if (at <= 0) return null;
    const own = rule.name.slice(0, at).trim();
    if (prefix === null) prefix = own;
    else if (own !== prefix) return null;
  }
  return prefix === "" ? null : prefix;
}

export function groupStageBanks(rules: readonly StageBankRule[]): StageBank[] {
  const banks: StageBank[] = [];
  const byAddress = new Map<number, StageBankRule[]>();
  for (const rule of rules) {
    const address = rule.trigger_type === undefined || rule.trigger_type === "knx" ? rule.knx_address_id : null;
    if (address === null || address === undefined) {
      banks.push({ key: `rule:${rule.id}`, label: rule.name, rules: [rule], fires: [rule] });
      continue;
    }
    const existing = byAddress.get(address);
    if (existing) {
      existing.push(rule);
      continue;
    }
    const members = [rule];
    byAddress.set(address, members);
    // Placed where the address's first rule is, so the row keeps the rules' order.
    banks.push({ key: `address:${address}`, label: rule.name, rules: members, fires: members });
  }
  return banks.map((bank) => {
    if (bank.rules.length < 2) return bank;
    const enabled = bank.rules.filter((rule) => rule.enabled !== false);
    return {
      ...bank,
      label: sharedPrefix(bank.rules) ?? bank.rules[0]?.name ?? bank.label,
      fires: enabled.length > 0 ? enabled : bank.rules,
    };
  });
}
