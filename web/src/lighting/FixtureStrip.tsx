/*
 * One fixture's strip in the Fixtures row (spec §21.11): a group-colour
 * accent at the top, then the fixture's own fader.
 */
import type { CSSProperties } from "react";

import type { SceneRing } from "@/components/fader/FaderStrip";

import { ChannelFader } from "./ChannelFader";
import type { LightingChannel } from "./types";

export interface FixtureStripProps {
  channel: LightingChannel;
  /** The colour of the first group this fixture belongs to, if any (identity only, §21.3). */
  accentColour?: string | undefined;
  sceneRing?: SceneRing | null;
  /** Forced read-only, independent of external control (§21.9's group tray, §18 Q3) — see `ChannelFader`. */
  readOnly?: boolean;
}

export function FixtureStrip({ channel, accentColour, sceneRing = null, readOnly = false }: FixtureStripProps) {
  const style = accentColour ? ({ "--accent-colour": accentColour } as CSSProperties) : undefined;
  return (
    <div className="lighting-fixture-strip" style={style}>
      <div className="lighting-fixture-accent" aria-hidden="true" />
      <ChannelFader channel={channel} sceneRing={sceneRing} readOnly={readOnly} />
    </div>
  );
}
