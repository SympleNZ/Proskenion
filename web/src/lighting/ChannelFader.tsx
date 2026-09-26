/*
 * One fixture's fader (spec §21.2 "FaderStrip subscribes to its own
 * channel", §21.11, §9.4, §7.2.7). This is the composition point: it
 * subscribes to exactly the live-store keys this one channel needs, computes
 * the ghost mark, and turns a drag into `beginGesture` / `send` /
 * `endGesture` against the store — `FaderStrip` itself knows none of that.
 */
import { linearLightingScale } from "@/components/fader/FaderScale";
import { FaderStrip, type SceneRing } from "@/components/fader/FaderStrip";
import { send } from "@/live/socket";
import { beginGesture, endGesture, levelKey, useColour, useControlsEnabled, useDisplayLevel, useExternalControl, useLevel, useMaster } from "@/live/store";

import { colourToCss } from "./colour";
import { compositeLevel } from "./compositeLevel";
import { isReadOnlyUnderExternalControl } from "./externalControl";
import type { LightingChannel } from "./types";
import { useGroupMultipliers } from "./useGroupMultipliers";

const scale = linearLightingScale();

export interface ChannelFaderProps {
  channel: LightingChannel;
  /** A scene driving this channel (§10.6, §21.11); nothing supplies this yet. */
  sceneRing?: SceneRing | null;
  /**
   * Forces the read-only rendering independent of external control — a
   * page's group tray whose member is reached only through the group while
   * the hirer configuration's individual-fixtures switch is off (§21.9,
   * §18 Q3). It never changes which value is shown, only whether a gesture
   * is accepted: outside external control the set and display values already
   * agree, and the ghost mark still needs the set value to composite against.
   */
  readOnly?: boolean;
}

export function ChannelFader({ channel, sceneRing = null, readOnly = false }: ChannelFaderProps) {
  const externalControl = useExternalControl();
  const controlsEnabled = useControlsEnabled();
  // The controller's own model — the thumb's "what was set" (§9.4) — kept
  // separate from the display rule below, because the ghost mark is always
  // computed against the *set* level, never against what is shown.
  const setLevel = useLevel(channel.id) ?? 0;
  // §7.2.7's display rule: observed while external control is active
  // (falling back to the controller's last value when nothing is observed,
  // which is exactly the manual-mode case), the controller's own model
  // otherwise.
  const displayLevel = useDisplayLevel(channel.id) ?? 0;
  const master = useMaster() ?? 100;
  const groupMultipliers = useGroupMultipliers(channel.group_ids);
  const colour = useColour(channel.id);

  // Only a DMX channel goes read-only under external control — the DMX pass
  // is what external control suspends. A knx_dimmer channel is house
  // lighting: composite_knx() runs normally throughout and is never gated
  // by DMX state (§7.2.3), so it stays interactive here.
  const externalReadOnly = isReadOnlyUnderExternalControl(channel, externalControl);
  const interactionReadOnly = externalReadOnly || readOnly;
  const value = externalReadOnly ? displayLevel : setLevel;
  // The ghost mark is "what the fixture is doing" under the controller's own
  // model; while a DMX channel is read-only under external control the
  // observed value already is that, live, so there is nothing left for a
  // ghost to add (§9.4, §7.2.7). A forced read-only carries no such
  // observed value, so its ghost mark still composites normally — that is
  // the whole point of a collapsed tray's member strips.
  const ghostValue = externalReadOnly ? null : compositeLevel(channel, setLevel, groupMultipliers, master);

  function handleChange(next: number | null): void {
    // linearLightingScale never actually produces null — a lighting level
    // has nothing to be "off" from — but shares FaderStrip's nullable signature.
    if (next === null) return;
    send("lighting", channel.id, next);
  }

  const gestureKey = levelKey(channel.id);

  return (
    <FaderStrip
      label={channel.name}
      value={value}
      scale={scale}
      onChange={handleChange}
      onGestureStart={() => beginGesture(gestureKey)}
      onGestureEnd={() => endGesture(gestureKey)}
      ghostValue={ghostValue}
      readOnly={interactionReadOnly}
      reduced={externalReadOnly && externalControl === "manual"}
      disabled={!controlsEnabled}
      fillColour={channel.has_colour && colour ? colourToCss(colour) : undefined}
      sceneRing={sceneRing}
      testId={`fixture-fader-${channel.id}`}
    />
  );
}
