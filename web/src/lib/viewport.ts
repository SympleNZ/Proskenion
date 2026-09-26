/*
 * Viewport queries the shell decides in JavaScript. The CSS breakpoints are
 * declared in styles/tokens.css; the two numbers here mirror the design
 * target of §21.9 (1920 × 1080), above which Display scale is offered.
 */
import { useSyncExternalStore } from "react";

export const DESIGN_TARGET = { width: 1920, height: 1080 } as const;

function subscribeResize(cb: () => void): () => void {
  window.addEventListener("resize", cb);
  return () => window.removeEventListener("resize", cb);
}

function atDesignTarget(): boolean {
  return window.innerWidth >= DESIGN_TARGET.width && window.innerHeight >= DESIGN_TARGET.height;
}

/** True at or above the 1920 × 1080 design target (§21.7 account menu, §21.9). */
export function useAtDesignTarget(): boolean {
  return useSyncExternalStore(subscribeResize, atDesignTarget, () => false);
}
