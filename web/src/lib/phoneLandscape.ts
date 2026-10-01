/*
 * Phone landscape, blocked outright (the owner's decision, 2026-09). §21.9's
 * own "Phone landscape" adapts the shell rather than refusing it — this is a
 * carve-out the coordinating session records against that section, not a
 * silent deviation (CONVENTIONS.md "Deviations").
 * Below this height in landscape, on a touch device, the interface has
 * nowhere left to give: §21.9 measures 37 px of fader travel there even after
 * the rail and compact furniture. Rather than ship that, the whole view is
 * covered by `PhoneLandscapeGuard`.
 *
 * Three conditions, all required, one query:
 *   orientation: landscape  rules out every portrait phone and both tablet
 *                            orientations, which are never blocked
 *   max-height: 500 px      phone landscape is ~390 to 430 px tall; the
 *                            smallest supported tablet landscape (an 11"
 *                            iPad, 1194×834) is 834, comfortably clear
 *   pointer: coarse         a short DESKTOP browser window (mouse) is never
 *                            blocked — only a touch device is
 *
 * `PHONE_LANDSCAPE_QUERY` is the one source for the boolean this hook
 * tracks; the overlay's own visibility is a plain CSS media query in
 * components.css (no flash on rotate — resolved before paint, not after a
 * React render), keeping the same 500 as a literal since `theme()` values
 * are not readable from JS. This hook exists only so `main.tsx` can `inert`
 * the app root while the overlay covers it, which CSS cannot do on its own.
 */
import { useSyncExternalStore } from "react";

export const PHONE_LANDSCAPE_MAX_HEIGHT_PX = 500;

export const PHONE_LANDSCAPE_QUERY = `(orientation: landscape) and (max-height: ${PHONE_LANDSCAPE_MAX_HEIGHT_PX}px) and (pointer: coarse)`;

function mediaQueryList(): MediaQueryList | null {
  return typeof window !== "undefined" && typeof window.matchMedia === "function" ? window.matchMedia(PHONE_LANDSCAPE_QUERY) : null;
}

function subscribe(onStoreChange: () => void): () => void {
  const mql = mediaQueryList();
  if (!mql) return () => {};
  // The modern listener; Safari before 14 only has the deprecated pair, but
  // every device this ships to (§5.1) has addEventListener — kept as a
  // fallback because it costs nothing.
  if (typeof mql.addEventListener === "function") {
    mql.addEventListener("change", onStoreChange);
    return () => mql.removeEventListener("change", onStoreChange);
  }
  mql.addListener(onStoreChange);
  return () => mql.removeListener(onStoreChange);
}

function getSnapshot(): boolean {
  return mediaQueryList()?.matches ?? false;
}

function getServerSnapshot(): boolean {
  return false;
}

/**
 * True while the device is a phone rotated to landscape (see above). Used
 * only to `inert`/`aria-hide` the app root while `PhoneLandscapeGuard`'s own
 * (CSS-driven) overlay covers it — never to decide the overlay's own
 * visibility, which must not wait for a render.
 */
export function usePhoneLandscape(): boolean {
  return useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);
}
