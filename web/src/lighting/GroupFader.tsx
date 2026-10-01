/*
 * One group's fader (spec §21.11; owner decision 2026-09-30, replacing §9.4's
 * multiplier). Dragging it sets **every member's level** to the dragged
 * value, as on a traditional desk; fixture faders then trim individually,
 * and the master scales the stage as a whole. The fader shows the row's
 * level: the level its members share, or the highest of them marked
 * "mixed" when they differ (`groupLevel.ts`). There is no ghost mark: a
 * group scales nothing, so there is nothing composited to show.
 *
 * External control suspends stage output, so the fader is read-only while
 * external control is active and the group holds a DMX fixture (§7.2.7,
 * §21.11). A group containing only KNX dimmers is unaffected by external
 * control and its fader stays live.
 *
 * The wire carries the level 0–100, exactly as a fixture's does
 * (`lighting_group`, `_group_write_handler`).
 *
 * Below the fader, the strip's BUMP (owner decision 2026-10-01): a flash to
 * full while held (`BumpButton.tsx`). It lights DMX members only — the
 * master's stage, not house lighting — so a group of KNX house dimmers alone
 * has none, and it is disabled whenever the fader is read-only under
 * external control. An indicator-only group has no strip at all, and a
 * caller that might show one passes `bump={false}`.
 */
import type { ReactNode } from "react";

import { linearLightingScale } from "@/components/fader/FaderScale";
import { cn } from "@/lib/utils";
import { FaderStrip } from "@/components/fader/FaderStrip";
import { send } from "@/live/socket";
import { beginGesture, endGesture, groupKey, useControlsEnabled, useExternalControl } from "@/live/store";

import { BumpButton } from "./BumpButton";
import { isGroupReadOnlyUnderExternalControl, type ExternalControlChannel } from "./externalControl";
import { useGroupLevel, type GroupMember } from "./groupLevel";

const scale = linearLightingScale();

export interface GroupFaderProps {
  groupId: number;
  label: string;
  /** The group's member channels: what the fader shows, and whether external control holds it. */
  members: readonly (ExternalControlChannel & GroupMember)[];
  /** The group's own colour along the card's top edge (identity only, §21.3). */
  accentColour?: string | undefined;
  className?: string;
  /** Below the readout: a tray's toggle, a member count. */
  children?: ReactNode;
  /** Offer the BUMP (default true). False for an indicator-only group, which has no fader either. */
  bump?: boolean;
}

export function GroupFader({ groupId, label, members, accentColour, className, children, bump = true }: GroupFaderProps) {
  const controlsEnabled = useControlsEnabled();
  const externalControl = useExternalControl();
  const { value, mixed } = useGroupLevel(groupId, members);
  const gestureKey = groupKey(groupId);
  const readOnly = isGroupReadOnlyUnderExternalControl(members, externalControl);
  const showBump = bump && members.some((member) => member.type === "dmx");

  function handleChange(next: number | null): void {
    // linearLightingScale never actually produces null — a lighting level
    // has nothing to be "off" from — but shares FaderStrip's nullable signature.
    if (next === null) return;
    send("lighting_group", groupId, next);
  }

  return (
    <FaderStrip
      className={cn("lighting-group-strip", className)}
      label={label}
      sublabel="group"
      accentColour={accentColour}
      value={value}
      scale={scale}
      onChange={handleChange}
      onGestureStart={() => beginGesture(gestureKey)}
      onGestureEnd={() => endGesture(gestureKey)}
      readoutNote={mixed ? "mixed" : undefined}
      readOnly={readOnly}
      disabled={!controlsEnabled}
      testId={`group-fader-${groupId}`}
    >
      {children}
      {showBump ? <BumpButton groupId={groupId} label={label} disabled={!controlsEnabled || readOnly} /> : null}
    </FaderStrip>
  );
}
