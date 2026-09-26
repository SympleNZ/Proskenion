/* Email (spec §21.24, §11.4, §4.6). Where alerts are sent. */
import type { HelpEntry } from "../types";

export const email = {
  "email.host": {
    term: "Host",
    body: "The SMTP relay's address.",
  },
  "email.port": {
    term: "Port",
    body: "The SMTP relay's port — 25 for an unauthenticated relay on the venue network is the usual case.",
  },
  "email.tls-mode": {
    term: "TLS mode",
    body: "None for a plain-text relay on the venue network only; Opportunistic to use STARTTLS if the relay offers it; TLS to connect already encrypted.",
  },
  "email.username": {
    term: "Username (optional)",
    body: "Leave blank for a relay that needs no login.",
  },
  "email.password": {
    term: "Password",
    body: "Leave blank to keep the stored password unchanged.",
  },
  "email.sender": {
    term: "Sender",
    body: "The From address alerts are sent as.",
  },
  "email.recipient": {
    term: "Recipient",
    body: "Where alerts are sent.",
  },
  "email.save": {
    term: "Save",
    body: "Saves this configuration. Also mirrors it to the emergency fallback file, so alerts can still fire if the main application cannot start.",
  },
  "email.remove": {
    term: "Remove mail settings",
    body: "Clears the SMTP relay and stops the firewall admitting it (§3.4). Alerts have nowhere to go until this is set up again.",
  },
} as const satisfies Record<string, HelpEntry>;
