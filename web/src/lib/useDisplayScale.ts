/*
 * The app-wide display scale (spec §21.9 "Display scale", §21.7 "The account
 * chip", B64) — the React side of `displayScale.ts`.
 *
 * One store for the page: the screen's size, whether the control applies
 * there, and the factor. It re-reads the screen on every window `resize`, so
 * dragging the window to another monitor (or docking a laptop) recalls that
 * screen's own stored value.
 *
 * `useApplyDisplayScale` is what makes it app-wide. The operator and admin
 * shells (`Shell.tsx`) mount it; it writes the factor to `--display-scale`
 * on `<html>`, where `html { zoom: var(--display-scale) }` (base.css) scales
 * everything together — nav, status bar, every view, and the sheets, menus,
 * popovers and toasts Radix and Sonner portal into `<body>` — so the layout
 * sees the smaller logical viewport, "no second code path". The hirer shell
 * never mounts it: the hirer has no display scale (§21.7).
 *
 * What `zoom` leaves to us (verified in Chromium; base.css and
 * components.css carry the matching rules):
 *   - Viewport units are not divided by the zoom, so `100dvh` under a 2×
 *     root is two screens tall. The shell's height divides by the factor.
 *   - `getBoundingClientRect` and pointer `clientX/Y` are both in physical
 *     (unzoomed) px, so ratio maths such as a fader's
 *     `(rect.bottom - clientY) / rect.height` is unaffected. A physical
 *     coordinate written back as a CSS length on a zoomed element is not —
 *     `toLogicalPx` converts it (the stage plan's long-press menu).
 *   - Floating UI (Radix popper) positions in physical px too; the popper
 *     wrapper is un-zoomed and its content re-zoomed (components.css).
 *   - An element's `clientWidth/Height` is logical, which is what the mixer
 *     fit and the panel layout want — but a factor change is not a window
 *     `resize`, so `DISPLAY_SCALE_EVENT` tells `useElementSize` to re-measure.
 */
import { useEffect, useSyncExternalStore } from "react";

import { clampDisplayScale, defaultDisplayScale, isDisplayScaleAvailable, readDisplayScale, writeDisplayScale } from "./displayScale";

/** Dispatched on `window` after the applied factor changes — a relayout that is not a `resize`. */
export const DISPLAY_SCALE_EVENT = "proskenion:display-scale";

export interface DisplayScaleSnapshot {
  /** The screen, in CSS px. */
  width: number;
  height: number;
  /** The control is offered here (at or above the design target). */
  available: boolean;
  /** The factor for this screen: always 1.0 where the control is not offered. */
  scale: number;
  /** The computed default for this screen (§21.9: 2.0× 4K, 1.3× 1440p, 1.0× otherwise). */
  defaultScale: number;
}

function readScreen(): { width: number; height: number } {
  const screenObject = typeof window !== "undefined" ? window.screen : undefined;
  return {
    width: screenObject?.width || window.innerWidth,
    height: screenObject?.height || window.innerHeight,
  };
}

function snapshotFor(width: number, height: number): DisplayScaleSnapshot {
  const available = isDisplayScaleAvailable(width, height);
  return {
    width,
    height,
    available,
    scale: available ? readDisplayScale(width, height) : 1,
    defaultScale: defaultDisplayScale(width, height),
  };
}

let snapshot: DisplayScaleSnapshot | null = null;
const listeners = new Set<() => void>();

function current(): DisplayScaleSnapshot {
  if (snapshot === null) {
    const { width, height } = readScreen();
    snapshot = snapshotFor(width, height);
  }
  return snapshot;
}

function emit(): void {
  for (const listener of listeners) listener();
}

function onResize(): void {
  const { width, height } = readScreen();
  const previous = current();
  if (previous.width === width && previous.height === height) return;
  snapshot = snapshotFor(width, height);
  emit();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  if (listeners.size === 1) window.addEventListener("resize", onResize);
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0) window.removeEventListener("resize", onResize);
  };
}

/** Sets and stores the factor for this screen; ignored where the control is not offered. */
export function setDisplayScale(value: number): void {
  const now = current();
  if (!now.available) return;
  const scale = writeDisplayScale(now.width, now.height, clampDisplayScale(value));
  if (scale === now.scale) return;
  snapshot = { ...now, scale };
  emit();
}

export function useDisplayScale(): DisplayScaleSnapshot {
  return useSyncExternalStore(subscribe, current, current);
}

/** The factor currently applied to the document — 1 when nothing is scaled (the hirer, the login page). */
let applied = 1;

export function appliedDisplayScale(): number {
  return applied;
}

/** A physical (`clientX`-space) length as a CSS length inside the scaled document. */
export function toLogicalPx(physical: number): number {
  return physical / applied;
}

function applyToDocument(scale: number): void {
  const root = document.documentElement;
  if (scale === 1) {
    root.style.removeProperty("--display-scale");
    delete root.dataset["displayScale"];
  } else {
    root.style.setProperty("--display-scale", String(scale));
    root.dataset["displayScale"] = String(scale);
  }
  if (scale !== applied) {
    applied = scale;
    window.dispatchEvent(new Event(DISPLAY_SCALE_EVENT));
  }
}

/**
 * Applies this screen's factor to the whole document while mounted and
 * `enabled` (the operator and admin shells). Unmounting — logging out, or
 * the hirer shell taking over — returns the document to 1.0×.
 */
export function useApplyDisplayScale(enabled: boolean): number {
  const { scale } = useDisplayScale();
  const effective = enabled ? scale : 1;
  useEffect(() => {
    applyToDocument(effective);
    return () => applyToDocument(1);
  }, [effective]);
  return effective;
}

/** Test seam: forget the cached screen so the next read sees a new one. */
export function resetDisplayScaleForTests(): void {
  snapshot = null;
  applyToDocument(1);
}
