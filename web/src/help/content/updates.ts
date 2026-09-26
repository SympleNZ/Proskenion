/* Updates (spec §21.24, §14). */
import type { HelpEntry } from "../types";

export const updates = {
  "updates.apply-os": {
    term: "Apply and reboot",
    body: "Reboots the appliance immediately into the new operating system on trial. If it is not healthy within ten minutes, or nothing confirms it, it reboots back into the current system automatically.",
  },
  "updates.apply-now": {
    term: "Apply now",
    body: "Applies this update immediately, rather than waiting for the next quiet moment.",
  },
  "updates.os.rollback": {
    term: "Roll back",
    body: "Reboots the appliance back into the previous operating system slot immediately, cancelling any trial in progress.",
  },
  "updates.restart": {
    term: "Restart application",
    body: "Restarts the Proskenion application only — the operating system and appliance are not affected. It will be unavailable for about a minute, and everyone connected is disconnected.",
  },
  "updates.reboot": {
    term: "Reboot appliance",
    body: "Reboots the whole appliance, including the operating system. It will be unavailable for about a minute, and everyone connected is disconnected.",
  },
} as const satisfies Record<string, HelpEntry>;
