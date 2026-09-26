/* Certificates (spec §21.24, §6.16, §3.2). */
import type { HelpEntry } from "../types";

export const certificates = {
  "certs.renew": {
    term: "Renew now",
    body: "Issues a fresh certificate through Let's Encrypt immediately, rather than waiting for the weekly automatic check.",
  },
  "certs.token": {
    term: "API token",
    body: "A Cloudflare API token scoped to one zone with DNS edit permission only. The stored value is never displayed — only whether one is set.",
  },
  "certs.token-save": {
    term: "Save",
    body: "Saves this token.",
  },
} as const satisfies Record<string, HelpEntry>;
