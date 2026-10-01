/*
 * A page's group master item (spec §21.9 "Group trays"). The page places a
 * master; it never defines membership (§15.9) — the server resolves
 * `members` and answers `tray`, the §21.9 contiguity/duplication check, so
 * this component only has to render what it is told:
 *
 *   tray === true   the master and its members share a tinted tray, with a
 *                    collapse that slides by class change (no measurement)
 *                    and a session-only toggle (never persisted, never
 *                    written to the server — §21.9 "the toggle lasts the
 *                    session")
 *   tray === false   an ordinary strip in the group's colour, with a small
 *                    member-count marker instead of a tray
 *
 * The collapse is a class/attribute change on a tray that is always mounted,
 * members included — "the toggle changes a class on the existing tray
 * rather than re-rendering the page: replacing the node mid-transition
 * would discard the animation" (§21.9). The expanded width is arithmetic —
 * member count × (strip + gap), carried to CSS as `--page-tray-count` — never
 * a measured one, so the transition causes no layout thrash.
 */
import { ChevronDown } from "lucide-react";
import { useState, type CSSProperties } from "react";

import { FixtureStrip } from "@/lighting/FixtureStrip";
import { GroupFader } from "@/lighting/GroupFader";
import { hasFader } from "@/lighting/indicatorGroups";
import type { LightingChannel } from "@/lighting/types";

import type { PageGroupMasterItem } from "./types";

export interface PageGroupItemProps {
  item: PageGroupMasterItem;
  /** Every lighting channel this page surface knows about, by id — members are looked up here (`GET /lighting/channels`). */
  channelsById: ReadonlyMap<number, LightingChannel>;
}

function memberCountLabel(count: number): string {
  return `${count} fixture${count === 1 ? "" : "s"}`;
}

export function PageGroupItem({ item, channelsById }: PageGroupItemProps) {
  // The page's stored opening state is where every mount starts; the toggle
  // below only ever changes this component's own state, never the server's
  // (§21.9) — "session-only" here means "for as long as this tray stays
  // mounted", which is what a component's own state already gives for free.
  const [expanded, setExpanded] = useState(item.expanded);
  const members = item.members
    .map((id) => channelsById.get(id))
    .filter((channel): channel is LightingChannel => channel !== undefined);
  const accentStyle = { "--accent-colour": item.group.colour } as CSSProperties;
  // The master's BUMP (owner decision 2026-10-01); never on an indicator-only group.
  const bump = hasFader(item.group);

  if (!item.tray) {
    return (
      <div className="page-item page-group-solo" data-testid={`page-group-${item.id}`}>
        <GroupFader groupId={item.group_id} label={item.group.name} members={members} accentColour={item.group.colour} bump={bump}>
          <p className="page-group-member-count">{memberCountLabel(item.members.length)}</p>
        </GroupFader>
      </div>
    );
  }

  const trayStyle = { ...accentStyle, "--page-tray-count": members.length } as CSSProperties;

  return (
    <div className="page-item page-group-tray" data-expanded={expanded} style={trayStyle} data-testid={`page-group-${item.id}`}>
      <p className="page-group-tray-name">{item.group.name}</p>
      <div className="page-group-tray-members" aria-hidden={!expanded} inert={!expanded}>
        {/* `members_writable` is present only for a hirer (Q3's individual-fixtures
            switch); absent — the operator's own pages — means writable, exactly as
            `PageLightingItem`'s own `writable` does for a standalone item. */}
        {members.map((member) => (
          <FixtureStrip key={member.id} channel={member} accentColour={item.group.colour} readOnly={item.members_writable === false} />
        ))}
      </div>
      <GroupFader
        groupId={item.group_id}
        label={item.group.name}
        members={members}
        accentColour={item.group.colour}
        className="page-group-tray-master"
        bump={bump}
      >
        <button
          type="button"
          className="page-group-tray-toggle"
          aria-expanded={expanded}
          onClick={() => setExpanded((value) => !value)}
        >
          <ChevronDown aria-hidden="true" className="chevron size-4" data-expanded={expanded} />
          {expanded ? "Collapse" : memberCountLabel(item.members.length)}
        </button>
      </GroupFader>
    </div>
  );
}
