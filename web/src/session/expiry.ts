/*
 * Session expiry decisions (spec §6.5, §21.8), kept pure so they test without
 * a DOM: the overlay when the session expired under 30 minutes ago, a full
 * redirect when longer, and "Access updated" for a revoked hirer.
 */
import type { SessionResponse } from "@/api/auth";

import type { Session } from "./context";

/** Expired under 30 minutes gives the overlay; over 30 minutes redirects (§6.5). */
export const REAUTH_WINDOW_MS = 30 * 60_000;

export function parseTime(iso: string | null | undefined): number | null {
  if (!iso) return null;
  const t = Date.parse(iso);
  return Number.isNaN(t) ? null : t;
}

export function sessionFromResponse(response: SessionResponse): { session: Session; offset: number } {
  const serverTime = parseTime(response.server_time);
  return {
    session: {
      tier: response.tier,
      expiresAt: parseTime(response.expires_at) ?? Date.now(),
      absoluteExpiresAt: parseTime(response.absolute_expires_at),
      certificate: response.certificate,
    },
    offset: serverTime === null ? 0 : serverTime - Date.now(),
  };
}

export type UnauthenticatedDecision = "expired" | "revoked" | "redirect";

/** The live socket closed with 4002: the absolute cap, which no overlay can extend. */
export const ABSOLUTE_EXPIRY_REASON = "absolute_expiry";

export function decideUnauthenticated(
  session: Session | null,
  reason: string | undefined,
  now: number = Date.now(),
): UnauthenticatedDecision {
  if (reason === "hirer_revoked") {
    // Only a hirer is ever revoked this way; a staff session is never shown
    // "Access updated" (§21.8), whatever arrives.
    return session !== null && session.tier !== "hirer" ? "redirect" : "revoked";
  }
  // Signing in again starts a new session, so the login screen — staff above,
  // the hirer's PIN below — rather than the re-authentication overlay.
  if (reason === ABSOLUTE_EXPIRY_REASON) return "redirect";
  if (!session) return "redirect";
  // A hirer re-enters a PIN, not a password, so the password overlay would mislead.
  if (session.tier === "hirer") return "redirect";
  return now - session.expiresAt < REAUTH_WINDOW_MS ? "expired" : "redirect";
}
