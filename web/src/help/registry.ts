/*
 * The merged help registry (spec §19.1). Each domain owns one file under
 * `content/`, reviewable on its own; this module only merges them and gives
 * the merged keys a type, so a `helpId` prop with a typo fails to compile
 * rather than silently showing nothing.
 */
import { backup } from "./content/backup";
import { certificates } from "./content/certificates";
import { devices } from "./content/devices";
import { email } from "./content/email";
import { health } from "./content/health";
import { hirer } from "./content/hirer";
import { knx } from "./content/knx";
import { lighting } from "./content/lighting";
import { logs } from "./content/logs";
import { mixer } from "./content/mixer";
import { network } from "./content/network";
import { pages } from "./content/pages";
import { rules } from "./content/rules";
import { scenes } from "./content/scenes";
import { updates } from "./content/updates";
import { users } from "./content/users";
import { video } from "./content/video";
import type { HelpEntry } from "./types";

export const HELP_CONTENT = {
  ...devices,
  ...lighting,
  ...knx,
  ...rules,
  ...scenes,
  ...pages,
  ...mixer,
  ...video,
  ...network,
  ...certificates,
  ...email,
  ...backup,
  ...updates,
  ...logs,
  ...health,
  ...hirer,
  ...users,
} as const satisfies Record<string, HelpEntry>;

export type HelpId = keyof typeof HELP_CONTENT;

export function getHelp(id: HelpId): HelpEntry {
  return HELP_CONTENT[id];
}
