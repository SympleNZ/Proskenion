/* Users (spec §21.23). */
import type { HelpEntry } from "../types";

export const users = {
  "users.admin.change": {
    term: "Change password (Admin)",
    body: "Opens a form to set a new admin password. Requires the current admin password.",
  },
  "users.operator.change": {
    term: "Change password (Operator)",
    body:
      "Opens a form to set a new operator password. Requires your own current admin password, " +
      "not the operator's — so this works even if the operator has forgotten theirs.",
  },
  "users.dialog.current-password": {
    term: "Current password",
    body: "The password proving you're allowed to make this change — always your own, whichever password is being replaced.",
  },
  "users.dialog.new-password": {
    term: "New password",
    body: "At least twelve characters.",
  },
  "users.dialog.confirm-password": {
    term: "Enter it again",
    body: "Typed a second time to catch a typo before it becomes the only copy.",
  },
  "users.dialog.submit": {
    term: "Change password",
    body: "Saves the new password. Every session for that tier on every other device is signed out at once.",
  },
} as const satisfies Record<string, HelpEntry>;
