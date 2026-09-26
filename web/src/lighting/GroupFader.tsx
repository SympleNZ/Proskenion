/*
 * One group's multiplier fader (spec §9.4, §21.11). A group multiplier
 * scales the group's stage (DMX) members only — KNX house dimmers are
 * outside it (§9.4, §9.5) — and external control suspends stage output, so
 * the fader is read-only while external control is active and the group
 * holds a DMX fixture (§7.2.7, §21.11). A group containing only KNX dimmers
 * is unaffected by external control and its fader stays live.
 *
 * Store and wire hold the multiplier as 0.0–1.0; the fader displays and
 * accepts 0–100%, so the conversion happens at this boundary, once.
 */
import { linearLightingScale } from "@/components/fader/FaderScale";
import { FaderStrip } from "@/components/fader/FaderStrip";
import { send } from "@/live/socket";
import { beginGesture, endGesture, groupKey, useControlsEnabled, useExternalControl, useGroup } from "@/live/store";

import { isGroupReadOnlyUnderExternalControl, type ExternalControlChannel } from "./externalControl";

const scale = linearLightingScale();

export interface GroupFaderProps {
  groupId: number;
  label: string;
  /** The group's member channels, which decide whether external control holds it. */
  members: readonly ExternalControlChannel[];
}

export function GroupFader({ groupId, label, members }: GroupFaderProps) {
  const controlsEnabled = useControlsEnabled();
  const externalControl = useExternalControl();
  const multiplier = useGroup(groupId) ?? 1;
  const value = multiplier * 100;
  const gestureKey = groupKey(groupId);

  function handleChange(next: number | null): void {
    // linearLightingScale never actually produces null — a group multiplier
    // has nothing to be "off" from — but shares FaderStrip's nullable signature.
    if (next === null) return;
    send("lighting_group", groupId, next / 100);
  }

  return (
    <FaderStrip
      label={label}
      value={value}
      scale={scale}
      onChange={handleChange}
      onGestureStart={() => beginGesture(gestureKey)}
      onGestureEnd={() => endGesture(gestureKey)}
      readOnly={isGroupReadOnlyUnderExternalControl(members, externalControl)}
      disabled={!controlsEnabled}
      testId={`group-fader-${groupId}`}
    />
  );
}
