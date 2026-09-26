/*
 * React binding for `displayScale.ts` (spec §21.9): reads the physical
 * screen once per mount, exposes whether the control applies, and mirrors
 * the current value onto a CSS custom property so `.page-surface-view` can
 * scale as one unit — "everything grows together — strips, type, buttons —
 * and the layout sees a smaller logical viewport", never a second code path.
 */
import { useCallback, useEffect, useState } from "react";

import { defaultDisplayScale, isDisplayScaleAvailable, readDisplayScale, writeDisplayScale } from "./displayScale";

export interface DisplayScaleState {
  available: boolean;
  scale: number;
  setScale: (value: number) => void;
}

function screenSize(): { width: number; height: number } {
  const screenObject = typeof window !== "undefined" ? window.screen : undefined;
  return {
    width: screenObject?.width ?? window.innerWidth,
    height: screenObject?.height ?? window.innerHeight,
  };
}

export function useDisplayScale(): DisplayScaleState {
  // The physical screen a booth touchscreen or monitor presents does not
  // change while the tab is open, so this is read once rather than tracked.
  const [{ width, height }] = useState(screenSize);
  const available = isDisplayScaleAvailable(width, height);
  const [scale, setScaleState] = useState(() => (available ? readDisplayScale(width, height) : defaultDisplayScale(width, height)));

  useEffect(() => {
    document.documentElement.style.setProperty("--page-display-scale", available ? String(scale) : "1");
    return () => {
      document.documentElement.style.removeProperty("--page-display-scale");
    };
  }, [available, scale]);

  const setScale = useCallback(
    (value: number) => {
      if (!available) return;
      setScaleState(writeDisplayScale(width, height, value));
    },
    [available, width, height],
  );

  return { available, scale, setScale };
}
