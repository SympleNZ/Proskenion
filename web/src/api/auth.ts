/* Authentication endpoints (spec §16.3). Tokens live in httpOnly cookies; the client never sees them. */
import { api } from "./client";

export type Tier = "admin" | "operator" | "hirer";

export const TIER_LABELS: Readonly<Record<Tier, string>> = {
  admin: "Admin",
  operator: "Operator",
  hirer: "Hire guest",
};

export interface LoginResponse {
  tier: Tier;
  expires_at: string;
}

/** Whether nginx serves a self-signed certificate; iOS then refuses the install (§6.16, §21.8). */
export type CertificateTrust = "trusted" | "self_signed";

export interface SessionResponse {
  tier: Tier;
  expires_at: string;
  absolute_expires_at: string;
  server_time: string;
  certificate: CertificateTrust;
}

export function login(password: string): Promise<LoginResponse> {
  return api<LoginResponse>("/auth/login", { body: { password }, quiet: true });
}

export function hirerLogin(pin: string): Promise<LoginResponse> {
  return api<LoginResponse>("/auth/hirer", { body: { pin }, quiet: true });
}

export function logout(): Promise<void> {
  return api<void>("/auth/logout", { method: "POST", quiet: true });
}

/** Re-issues the cookie. Called every 5 minutes only while the page is visible (§6.4). */
export function getSession(options: { quiet?: boolean; signal?: AbortSignal } = {}): Promise<SessionResponse> {
  return api<SessionResponse>("/auth/session", options);
}

/** Where a tier lands after signing in (§6.13). */
export function homeFor(tier: Tier): string {
  return tier === "hirer" ? "/hire" : "/app";
}
