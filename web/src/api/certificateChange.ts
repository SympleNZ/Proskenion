/*
 * "The controller's certificate has just changed" — noted when a response
 * says so (the first-run wizard's certificate step, a backup restore that
 * brought a different certificate back), and read by whatever has to explain
 * what happens next.
 *
 * A browser that accepted the old self-signed certificate refuses the new
 * one at TLS: the next request never reaches nginx, and fetch reports it as
 * a plain network failure. Without this, that failure reads "Could not reach
 * the controller" (the rebuilt appliance, 25 September 2026). And a
 * certificate that does not name the address the page is on — the bare IP,
 * after a restore brings the real certificate back — can never be accepted
 * there at all, so the user has to be told which address to use instead.
 */
import { useSyncExternalStore } from "react";

export interface CertificateChange {
  /** The names and addresses the new certificate is valid for, as the server listed them. */
  names: readonly string[];
  /** Client clock, when the response saying so arrived. */
  at: number;
}

type Listener = () => void;

let current: CertificateChange | null = null;
const listeners = new Set<Listener>();

function emit(): void {
  for (const listener of [...listeners]) listener();
}

export function noteCertificateChange(names: readonly string[], at: number = Date.now()): void {
  current = Object.freeze({ names: Object.freeze([...names]), at });
  emit();
}

export function clearCertificateChange(): void {
  if (current === null) return;
  current = null;
  emit();
}

export function getCertificateChange(): CertificateChange | null {
  return current;
}

export function subscribeCertificateChange(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function useCertificateChange(): CertificateChange | null {
  return useSyncExternalStore(subscribeCertificateChange, getCertificateChange, getCertificateChange);
}

/** Whether a certificate valid for `names` covers `host` (a DNS name or an address). */
export function hostCovered(host: string, names: readonly string[]): boolean {
  const wanted = host.toLowerCase().replace(/^\[|\]$/g, "");
  return names.some((raw) => {
    const name = raw.toLowerCase();
    if (name === wanted) return true;
    // A wildcard covers exactly one label: *.school.nz covers av.school.nz only.
    if (name.startsWith("*.")) {
      const rest = name.slice(1);
      return wanted.endsWith(rest) && !wanted.slice(0, -rest.length).includes(".") && wanted.length > rest.length;
    }
    return false;
  });
}

/** The name to send the user to: the first DNS name the certificate carries, else its first entry. */
export function preferredName(names: readonly string[]): string | null {
  const dns = names.find((name) => !/^[\d.]+$/.test(name) && !name.includes(":") && !name.startsWith("*."));
  return dns ?? names[0] ?? null;
}

export const CERTIFICATE_CHANGED_MESSAGE =
  "The controller's certificate has changed. Reload this page and accept the new certificate.";

/** What a request that failed at the network level means, right after a certificate change. */
export const CERTIFICATE_REFUSED_MESSAGE =
  "The browser refused the controller's new certificate, so the request never reached it. Reload this page and accept the new certificate.";
