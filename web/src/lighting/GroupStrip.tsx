/*
 * One group's strip in the Groups row (spec §21.11). Colour identity at the
 * top (never status, §21.3) and the group's own fader, which sets its
 * members' levels and shows the row's level (owner decision 2026-09-30).
 *
 * The strip's BUMP (owner decision 2026-10-01) is the fader's own
 * (`GroupFader`); an indicator-only group never reaches this row, and is
 * refused a BUMP here as well in case it ever does.
 *
 * There is no "held by" hint any more: it named the group whose higher
 * multiplier was holding this one's members, and groups no longer multiply.
 */
import { GroupFader } from "./GroupFader";
import { hasFader } from "./indicatorGroups";
import type { LightingChannel, LightingGroup } from "./types";

export interface GroupStripProps {
  group: LightingGroup;
  /** This group's member channels. */
  channels: readonly LightingChannel[];
}

export function GroupStrip({ group, channels }: GroupStripProps) {
  return <GroupFader groupId={group.id} label={group.name} members={channels} accentColour={group.colour} bump={hasFader(group)} />;
}
