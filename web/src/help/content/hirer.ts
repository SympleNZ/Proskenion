/* Hirer access (spec §21.20, §6.6, §15.4). */
import type { HelpEntry } from "../types";

export const hirer = {
  "hirer.save": {
    term: "Save changes",
    body: "Saves which pages and channel ceilings a hirer can reach.",
  },
  "hirer.pin.set": {
    term: "Set PIN",
    body: "Sets the six-digit PIN a hirer signs in with. Signs out every hirer connected right now.",
  },
  "hirer.access-and-pin": {
    term: "Access and PIN",
    body: "Change PIN sets a new six-digit sign-in code and signs out every hirer connected right now. Hire guest access enabled is the kill switch — turning it off drops every connected hirer immediately and blocks new sign-ins until it is turned back on.",
  },
  "hirer.lighting-control": {
    term: "Lighting control",
    body: "Three separate switches: whether a hirer can touch lighting at all, whether they can move an individual fixture within a group (rather than only its group master), and whether they can change colour. Each is independent of the others.",
  },
} as const satisfies Record<string, HelpEntry>;
