/*
 * Ceiling conflict warnings (docs/plans/phase-5-contracts.md "Hirer
 * configuration", `GET /hirer/conflicts`; spec §21.20, §16.6). Worth catching
 * before a hire rather than during one: a desk scene or scene action that
 * sets a channel above its ceiling clamps the instant it runs, which reads as
 * a fault to whoever is in the room at the time.
 */
import { formatDb } from "@/lib/faderLaw";

import type { HirerConflict } from "./types";

/** One line explaining what will clamp, and when — no React, so it is unit-testable on its own. */
export function conflictMessage(conflict: HirerConflict): string {
  const { source, channel_name, ceiling_db } = conflict;
  if (source.kind === "desk_scene") {
    if (conflict.observed === false) {
      return (
        `“${source.name}” has never been recalled, so its stored level for ${channel_name} is ` +
        `not yet checked — recall it once to check it against the ${formatDb(ceiling_db)} dB ceiling.`
      );
    }
    return (
      `“${source.name}” recalls ${channel_name} at ${formatDb(conflict.level_db)} dB, above the ` +
      `${formatDb(ceiling_db)} dB ceiling — the fader will be clamped immediately after the scene is recalled.`
    );
  }
  return (
    `“${source.name}” sets ${channel_name} to ${formatDb(conflict.level_db)} dB, above the ` +
    `${formatDb(ceiling_db)} dB ceiling, when this scene runs.`
  );
}

/** A stable React key: a channel can appear in more than one conflict, so the id alone is not unique. */
export function conflictKey(conflict: HirerConflict): string {
  const source =
    conflict.source.kind === "desk_scene" ? `desk_scene:${conflict.source.desk_scene_id}` : `scene_action:${conflict.source.action_id}`;
  return `${conflict.channel_id}:${source}`;
}
