/*
 * The stage banks row (spec §7.1, §21.11): one button per wall-panel switch
 * — the `lighting_group` rules sharing a KNX trigger address, grouped by
 * `groupStageBanks` (owner decision 2026-09-30) — its lamp following the
 * KNX panel state carried in the live store's `bindings`, pressed over REST
 * since it is a discrete action, not a drag (§21.2).
 *
 * A press recalls each rule's group: it sets every member's level, exactly
 * as that group's fader would (§8.8). So a bank is locked out under external
 * control exactly when one of its groups' levels is read-only
 * (`isGroupReadOnlyUnderExternalControl`): when the group holds a DMX
 * fixture. A bank whose groups contain only KNX dimmers is unaffected by
 * external control and stays live, as the controller still fires its
 * bindings (§7.2.7: house lighting is never gated).
 */
import { useMemo, useSyncExternalStore } from "react";
import { Zap } from "lucide-react";

import { bindingKey, getBinding, subscribeKey, useExternalControl } from "@/live/store";

import { useFireRule, useLightingChannels, useLightingGroups, useStageBankRules } from "./api";
import { isGroupReadOnlyUnderExternalControl, type ExternalControlChannel } from "./externalControl";
import { groupStageBanks, type StageBank } from "./stageBankGroups";

/** Whether every one of `ruleIds`' bindings is on — one boolean, per-key subscriptions (§21.2). */
function useAllBindingsOn(ruleIds: readonly number[]): boolean {
  const idsKey = ruleIds.join(",");
  const subscribe = useMemo(
    () => (onStoreChange: () => void) => {
      const unsubscribes = ruleIds.map((id) => subscribeKey(bindingKey(id), onStoreChange));
      return () => {
        for (const unsubscribe of unsubscribes) unsubscribe();
      };
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [idsKey],
  );
  const getSnapshot = useMemo(
    () => () => ruleIds.length > 0 && ruleIds.every((id) => getBinding(id)),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [idsKey],
  );
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}

function BankButton({ bank, lockedOut }: { bank: StageBank; lockedOut: boolean }) {
  const active = useAllBindingsOn(bank.rules.map((rule) => rule.id));
  const fire = useFireRule();
  function press(): void {
    const value = active ? 0 : 1;
    for (const rule of bank.fires) fire.mutate({ id: rule.id, value });
  }
  return (
    <button
      type="button"
      className="lighting-bank"
      aria-pressed={active}
      disabled={lockedOut || fire.isPending}
      onClick={press}
    >
      <span className="lighting-bank-lamp" aria-hidden="true" />
      {bank.label}
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
  const banks = useMemo(() => groupStageBanks(data?.rules ?? []), [data]);
  if (banks.length === 0) return null;
  const isLockedOut = (bank: StageBank): boolean =>
    bank.rules.some((rule) => isGroupReadOnlyUnderExternalControl(members?.get(rule.lighting_group_id), externalControl));
  return (
    <section className="lighting-banks">
      <h2 className="lighting-section-title">Stage banks</h2>
      <div className="lighting-banks-row">
        {banks.map((bank) => (
          <BankButton key={bank.key} bank={bank} lockedOut={isLockedOut(bank)} />
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
