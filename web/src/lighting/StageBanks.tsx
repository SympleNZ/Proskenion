/*
 * The stage banks row (spec §7.1, §21.11): each button is a `lighting_group`
 * rule, its lamp following the KNX panel state carried in the live store's
 * `bindings` (added to `applyLighting` for this task), pressed over REST
 * since it is a discrete action, not a drag (§21.2).
 *
 * A press recalls the rule's group: it forces the group multiplier to full
 * and sets every member's level (§8.8). So a bank is locked out under
 * external control exactly when its group's level is read-only
 * (`isGroupReadOnlyUnderExternalControl`): when the group holds a DMX
 * fixture. A bank whose group contains only KNX dimmers is unaffected by
 * external control and stays live, as the controller still fires its
 * binding (§7.2.7: house lighting is never gated).
 */
import { useMemo } from "react";
import { Zap } from "lucide-react";

import { useBinding, useExternalControl } from "@/live/store";

import { useFireRule, useLightingChannels, useLightingGroups, useStageBankRules } from "./api";
import { isGroupReadOnlyUnderExternalControl, type ExternalControlChannel } from "./externalControl";
import type { StageBankRule } from "./types";

function BankButton({ rule, lockedOut }: { rule: StageBankRule; lockedOut: boolean }) {
  const active = useBinding(rule.id);
  const fire = useFireRule();
  return (
    <button
      type="button"
      className="lighting-bank"
      aria-pressed={active}
      disabled={lockedOut || fire.isPending}
      onClick={() => fire.mutate({ id: rule.id, value: active ? 0 : 1 })}
    >
      <span className="lighting-bank-lamp" aria-hidden="true" />
      {rule.name}
    </button>
  );
}

/** Each group's member channels, or `undefined` until the configuration has loaded. */
function useGroupMembers(): ReadonlyMap<number, readonly ExternalControlChannel[]> | undefined {
  const channels = useLightingChannels();
  const groups = useLightingGroups();
  return useMemo(() => {
    if (!channels.data || !groups.data) return undefined;
    const byId = new Map(channels.data.channels.map((channel) => [channel.id, channel] as const));
    return new Map(
      groups.data.groups.map((group) => [
        group.id,
        group.channel_ids.flatMap((id) => {
          const channel = byId.get(id);
          return channel ? [channel] : [];
        }),
      ]),
    );
  }, [channels.data, groups.data]);
}

export function StageBanks() {
  const { data } = useStageBankRules();
  const externalControl = useExternalControl();
  const members = useGroupMembers();
  const banks = data?.rules ?? [];
  if (banks.length === 0) return null;
  const isLockedOut = (rule: StageBankRule): boolean =>
    isGroupReadOnlyUnderExternalControl(members?.get(rule.lighting_group_id), externalControl);
  return (
    <section className="lighting-banks">
      <h2 className="lighting-section-title">Stage banks</h2>
      <div className="lighting-banks-row">
        {banks.map((rule) => (
          <BankButton key={rule.id} rule={rule} lockedOut={isLockedOut(rule)} />
        ))}
      </div>
      {banks.some(isLockedOut) ? (
        <p className="lighting-lockout">
          <Zap aria-hidden="true" className="size-4" />
          Locked out — external control active
        </p>
      ) : null}
    </section>
  );
}
