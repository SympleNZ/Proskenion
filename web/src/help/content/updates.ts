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
    term: "Restart services",
    body: "Restarts the controller software only, for when the screens respond but something is stuck. The operating system keeps running. Control is unavailable for about a minute, and everyone connected is disconnected.",
  },
  "updates.reboot": {
    term: "Restart controller",
    body: "Restarts the whole controller, operating system included, and it comes back on by itself; nobody needs to go to the rack. Control is unavailable for about a minute, and everyone connected is disconnected.",
  },
  "updates.shutdown": {
    term: "Shut down",
    body: "Powers the controller off cleanly. It does not come back on by itself: switch its power off and on at the rack (or unplug and replug it) to start it again. Lighting, sound and video control stop until then.",
  },
} as const satisfies Record<string, HelpEntry>;
