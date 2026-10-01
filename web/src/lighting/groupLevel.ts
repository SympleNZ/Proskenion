/*
 * What a group fader shows (owner decision 2026-09-30, replacing §9.4's
 * multiplier). A group fader sets every member's level, as on a traditional
 * desk, and fixture faders then trim individually. A group has no stored
 * value of its own, so its fader is derived from its members' levels — the
 * live store already holds every one of them, DMX and KNX alike:
 *
 *   - every member at one level L — the fader shows L. "At one level" allows
 *     for each member's own range: a group set to 90 leaves a fixture capped
 *     at 80 at 80, and a fixture with a 10 % floor holds 10 when the group is
 *     at 0; neither is "mixed", because that is exactly what the group set.
 *   - anything else — the fader shows the **highest** member level, marked
 *     "mixed" beside the readout.
 *
 * While the fader is being dragged, its own pending value (the store's
 * `groupKey`) is shown instead, so the thumb stays under the finger until the
 * write is acknowledged and the members' levels arrive.
 */
import { useMemo, useSyncExternalStore } from "react";

import { getGroup, getLevel, groupKey, hasPending, levelKey, subscribeKey } from "@/live/store";

/** A member as far as its group's fader cares: its id and its own range. */
export interface GroupMember {
  readonly id: number;
  readonly min_value: number;
  readonly max_value: number;
}

export interface GroupLevel {
  /** 0–100: the level every member is at, or the highest member's when mixed. */
  readonly value: number;
  readonly mixed: boolean;
}

/** Levels within this are the same level (one decimal on the wire, §9.2). */
const TOLERANCE = 0.05;

function clamp(level: number, member: GroupMember): number {
  return Math.min(member.max_value, Math.max(member.min_value, level));
}

/**
 * The group's level from its members' levels (`null` — nothing stored yet —
 * reads as 0, the level store's own default). A group with no members is at 0.
 * Pure, so the rule is testable without a store.
 */
export function groupLevel(members: readonly GroupMember[], levels: readonly (number | null)[]): GroupLevel {
  if (members.length === 0) return { value: 0, mixed: false };
  const actual = levels.map((level) => level ?? 0);
  // A level the group could have set to leave every member where it is: try
  // each member's own level as the group's, highest first so the answer is
  // the one the fader would have been dragged to.
  const candidates = [...new Set(actual)].sort((a, b) => b - a);
  for (const candidate of candidates) {
    if (members.every((member, index) => Math.abs(clamp(candidate, member) - (actual[index] ?? 0)) <= TOLERANCE)) {
      return { value: candidate, mixed: false };
    }
  }
  return { value: Math.max(...actual), mixed: true };
}

/**
 * `groupLevel` over the live store, re-rendering only when a member's level
 * or the fader's own pending value changes (§21.2: per-key subscriptions,
 * a primitive snapshot). The snapshot is the pair encoded as one string, so
 * `useSyncExternalStore` compares it by value.
 */
export function useGroupLevel(groupId: number, members: readonly GroupMember[]): GroupLevel {
  // Membership is configuration, not live state: the ids and ranges are a
  // stable key for the subscription and the snapshot function.
  const membersKey = members.map((m) => `${m.id}:${m.min_value}:${m.max_value}`).join(",");

  const subscribe = useMemo(
    () => (onStoreChange: () => void) => {
      const unsubscribes = [groupKey(groupId), ...members.map((m) => levelKey(m.id))].map((key) =>
        subscribeKey(key, onStoreChange),
      );
      return () => {
        for (const unsubscribe of unsubscribes) unsubscribe();
      };
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [groupId, membersKey],
  );

  const getSnapshot = useMemo(
    () => () => {
      const dragging = hasPending(groupKey(groupId)) ? getGroup(groupId) : null;
      if (dragging !== null) return `${dragging}|0`;
      const { value, mixed } = groupLevel(
        members,
        members.map((m) => getLevel(m.id)),
      );
      return `${value}|${mixed ? 1 : 0}`;
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [groupId, membersKey],
  );

  const snapshot = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
  return useMemo(() => {
    const [value, mixed] = snapshot.split("|");
    return { value: Number(value), mixed: mixed === "1" };
  }, [snapshot]);
}
