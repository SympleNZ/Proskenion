/*
 * The handoff to `/reconnect` (spec §10.8, contracts §5 steps 2–4) and what
 * the Network screen remembers about it across that round trip.
 *
 * `/reconnect` is a static page served from the root image on port 80, not
 * this application (`appliance/share/auditorium/reconnect/index.html`) — it
 * polls the new address's `/health` and then redirects the browser on:
 * `https://{hostname}/` when a Cloudflare token already pointed the
 * hostname at the new address (`dns_updated`), otherwise
 * `https://{address}/`.
 *
 * `confirm_token` travels the whole journey in the URL **fragment**, never
 * the query string (phase-6-contracts.md, wave 3 additions): a fragment is
 * never sent in an HTTP request at all, so it never reaches nginx's access
 * log or a `Referer` header, on either hop. `reconnect/index.html` reads it
 * off its own arrival, then appends it to the fragment of the address it
 * redirects to; this application reads it back off *its* arrival
 * (`takeArrivalToken`, called once from `App.tsx` — before any tier-guard
 * redirect can drop it, and before a sign-in is even needed) and stashes it
 * in `sessionStorage` so it survives whatever routing or re-authentication
 * happens next, however many renders that takes.
 *
 * This is what makes confirming work from a *different* origin, not only
 * the same-origin case `StoredPendingChange` below was already good for
 * (§10.8's `dns_updated` path, where the admin reaches the controller by
 * its hostname both before and after — the common case with a Cloudflare
 * token configured, §3.2). `StoredPendingChange` remains the fallback for a
 * browser that already knows the pending change without having arrived via
 * a fresh `#confirm_token=…` — reopening the Network screen later in the
 * same tab, say.
 */
import type { NetworkChangeResult } from "./types";

const STORAGE_KEY = "proskenion.network.pending-confirm";
const ARRIVAL_STORAGE_KEY = "proskenion.network.arrival-token";
const FRAGMENT_PARAM = "confirm_token";

export interface StoredPendingChange {
  confirmToken: string;
  appliedAt: string;
  revertsAt: string;
  address: string;
  hostname: string;
}

export function storePendingChange(storage: Storage, change: StoredPendingChange): void {
  try {
    storage.setItem(STORAGE_KEY, JSON.stringify(change));
  } catch {
    // Private browsing, or storage disabled — the reconnect flow itself
    // does not depend on this; it only loses the "Confirm" shortcut.
  }
}

function isStoredPendingChange(value: unknown): value is StoredPendingChange {
  if (!value || typeof value !== "object") return false;
  const v = value as Partial<StoredPendingChange>;
  return (
    typeof v.confirmToken === "string" &&
    typeof v.appliedAt === "string" &&
    typeof v.revertsAt === "string" &&
    typeof v.address === "string" &&
    typeof v.hostname === "string"
  );
}

export function readStoredPendingChange(storage: Storage): StoredPendingChange | null {
  try {
    const raw = storage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed: unknown = JSON.parse(raw);
    return isStoredPendingChange(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

export function clearStoredPendingChange(storage: Storage): void {
  try {
    storage.removeItem(STORAGE_KEY);
  } catch {
    // Nothing to clear if storage never worked in the first place.
  }
}

/** The URL the browser is sent to (contracts §5 step 2): the token rides in the fragment. */
export function reconnectUrl(origin: string, change: NetworkChangeResult): string {
  const params = new URLSearchParams({
    address: change.address,
    hostname: change.hostname,
    dns_updated: change.dns_updated ? "1" : "0",
  });
  return `http://${origin}/reconnect?${params.toString()}#${FRAGMENT_PARAM}=${encodeURIComponent(change.confirm_token)}`;
}

/** Parse `confirm_token` out of a `location.hash`-shaped string (`""`, `"#"`, or `"#confirm_token=…"`). */
export function parseFragmentToken(hash: string): string | null {
  const trimmed = hash.startsWith("#") ? hash.slice(1) : hash;
  if (!trimmed) return null;
  const params = new URLSearchParams(trimmed);
  const token = params.get(FRAGMENT_PARAM);
  return token && token.length > 0 ? token : null;
}

/**
 * Consume the fragment token off the current page, if there is one: read it,
 * then strip it from the visible URL immediately (`history.replaceState`) so
 * it never sits in the address bar, browser history or a screenshot. Reading
 * is non-destructive and safe to call more than once (a React lazy
 * initialiser may run twice under StrictMode) — it is the strip that makes a
 * second call see nothing, which is exactly the "take" semantics this needs:
 * once consumed, it is gone.
 */
export function takeArrivalToken(
  loc: Pick<Location, "hash" | "pathname" | "search">,
  hist: Pick<History, "replaceState">,
): string | null {
  const token = parseFragmentToken(loc.hash);
  if (token !== null) {
    hist.replaceState(null, "", loc.pathname + loc.search);
  }
  return token;
}

export function storeArrivalToken(storage: Storage, token: string): void {
  try {
    storage.setItem(ARRIVAL_STORAGE_KEY, token);
  } catch {
    // As above: this only loses the auto-confirm convenience, not correctness.
  }
}

export function readArrivalToken(storage: Storage): string | null {
  try {
    return storage.getItem(ARRIVAL_STORAGE_KEY);
  } catch {
    return null;
  }
}

export function clearArrivalToken(storage: Storage): void {
  try {
    storage.removeItem(ARRIVAL_STORAGE_KEY);
  } catch {
    // Nothing to clear if storage never worked in the first place.
  }
}
