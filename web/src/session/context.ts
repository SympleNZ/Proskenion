/* Session context (spec §6.4, §6.5): what any component may ask about who is signed in. */
import { createContext, useContext } from "react";

import type { CertificateTrust, LoginResponse, Tier } from "@/api/auth";

export type SessionStatus = "loading" | "anonymous" | "authenticated";

export interface Session {
  tier: Tier;
  /** Epoch ms. */
  expiresAt: number;
  absoluteExpiresAt: number | null;
  /** Whether nginx serves a self-signed certificate (§6.16, §21.8) — the install prompt suppresses itself over one. */
  certificate: CertificateTrust;
}

export type SessionOverlay = "expired" | "revoked" | null;

export interface SessionContextValue {
  status: SessionStatus;
  session: Session | null;
  /** Appliance clock minus browser clock, in ms (§21.7). */
  serverTimeOffset: number;
  overlay: SessionOverlay;
  /** Adopt a fresh login response. */
  signIn(response: LoginResponse): void;
  /** GET /auth/session; re-issues the cookie. */
  refresh(): Promise<void>;
  signOut(): Promise<void>;
}

export const SessionContext = createContext<SessionContextValue | null>(null);

export function useSession(): SessionContextValue {
  const ctx = useContext(SessionContext);
  if (!ctx) throw new Error("useSession must be used inside SessionProvider");
  return ctx;
}
