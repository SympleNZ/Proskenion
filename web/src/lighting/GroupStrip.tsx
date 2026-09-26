/*
 * One group's strip in the Groups row (spec §21.11, §9.4, §7.2.3). Colour
 * identity at the top (never status, §21.3), the group's own fader, and the
 * dominated-group hint naming whichever other group is actually holding its
 * members.
 */
import { useMemo, type CSSProperties } from "react";

import { dominatedGroupHint, type DominatedChannel } from "./dominance";
import { GroupFader } from "./GroupFader";
import type { LightingGroup } from "./types";
import { useGroupMultipliers } from "./useGroupMultipliers";

export interface GroupStripProps {
  group: LightingGroup;
  /** This group's member channels, each carrying every group it belongs to. */
  channels: readonly DominatedChannel[];
  groupNames: ReadonlyMap<number, string>;
}

export function GroupStrip({ group, channels, groupNames }: GroupStripProps) {
  const relevantGroupIds = useMemo(() => {
    const ids = new Set<number>([group.id]);
    for (const channel of channels) {
      for (const id of channel.group_ids) ids.add(id);
    }
    return [...ids].sort((a, b) => a - b);
  }, [group.id, channels]);

  const multipliers = useGroupMultipliers(relevantGroupIds);
  const hint = dominatedGroupHint({ groupId: group.id, channels, groupMultipliers: multipliers, groupNames });

  return (
    <div className="lighting-group-strip" style={{ "--accent-colour": group.colour } as CSSProperties}>
      <div className="lighting-group-accent" aria-hidden="true" />
      <GroupFader groupId={group.id} label={group.name} members={channels} />
      {hint ? (
        <p className="lighting-dominated-hint">
          {hint.count} fixture{hint.count === 1 ? "" : "s"} held by {hint.dominatingGroupName}
        </p>
      ) : null}
    </div>
  );
}
