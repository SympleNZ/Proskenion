/*
 * The Fade time shared between the Lighting view's header control and the
 * stage plan's multi-select "Set level" action (spec §21.11, §21.12). §21.12
 * says "Set level" on the stage plan "uses the Lighting view's Fade time" —
 * one duration, read by both views, not two independent copies that could
 * disagree.
 *
 * This is neither of CONVENTIONS "Interface"'s two state categories: nothing
 * server-side ever holds it (so it is not TanStack Query), and it is not
 * driven by WebSocket frames (so it is not `live/store.ts`). It is a small
 * piece of shared UI state with exactly one consumer pattern — read the
 * current value, be notified when it changes — so it gets the same
 * `useSyncExternalStore` primitive the live store uses, at a scale of one
 * value rather than fifty keys.
 */
import { useSyncExternalStore } from "react";

/** Mirrors the range LightingHeader's slider already used (§21.11). */
export const FADE_MIN_S = 0;
export const FADE_MAX_S = 10;
export const FADE_STEP_S = 0.1;
export const FADE_DEFAULT_S = 2;

function clampSeconds(seconds: number): number {
  if (!Number.isFinite(seconds)) return FADE_DEFAULT_S;
  return Math.min(FADE_MAX_S, Math.max(FADE_MIN_S, seconds));
}

let fadeSeconds = FADE_DEFAULT_S;
const listeners = new Set<() => void>();

function notify(): void {
  for (const listener of [...listeners]) listener();
}

export function getFadeSeconds(): number {
  return fadeSeconds;
}

export function setFadeSeconds(seconds: number): void {
  const clamped = clampSeconds(seconds);
  if (clamped === fadeSeconds) return;
  fadeSeconds = clamped;
  notify();
}

/** Seconds → whole milliseconds, the unit `POST .../levels` and `.../level` carry. */
export function fadeMs(): number {
  return Math.round(fadeSeconds * 1000);
}

export function useFadeSeconds(): number {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    getFadeSeconds,
    getFadeSeconds,
  );
}

/** Test helper: back to the default with listeners still attached. */
export function resetFadeSeconds(): void {
  if (fadeSeconds === FADE_DEFAULT_S) return;
  fadeSeconds = FADE_DEFAULT_S;
  notify();
}
