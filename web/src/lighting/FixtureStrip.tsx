/*
 * One fixture's strip in the Fixtures row (spec §21.11): the shared channel
 * strip card, its top edge in the colour of the fixture's first group
 * (identity only, §21.3), with the fixture's own fader.
 */
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
  return <ChannelFader channel={channel} accentColour={accentColour} sceneRing={sceneRing} readOnly={readOnly} />;
}
