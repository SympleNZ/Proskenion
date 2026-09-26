import { renderHook } from "@testing-library/react";
import { act } from "react";
import { beforeEach, describe, expect, it } from "vitest";

import { FADE_DEFAULT_S, fadeMs, getFadeSeconds, resetFadeSeconds, setFadeSeconds, useFadeSeconds } from "./fadeTime";

beforeEach(() => {
  resetFadeSeconds();
});

describe("fadeTime — the value shared between the Lighting view and the stage plan (§21.12)", () => {
  it("starts at the default", () => {
    expect(getFadeSeconds()).toBe(FADE_DEFAULT_S);
  });

  it("clamps to the 0–10 s range", () => {
    setFadeSeconds(-5);
    expect(getFadeSeconds()).toBe(0);
    setFadeSeconds(50);
    expect(getFadeSeconds()).toBe(10);
  });

  it("fadeMs converts seconds to whole milliseconds", () => {
    setFadeSeconds(2.5);
    expect(fadeMs()).toBe(2500);
  });

  it("useFadeSeconds re-renders every subscriber when the value changes — one shared value, not a copy per view", () => {
    const a = renderHook(() => useFadeSeconds());
    const b = renderHook(() => useFadeSeconds());
    expect(a.result.current).toBe(FADE_DEFAULT_S);
    expect(b.result.current).toBe(FADE_DEFAULT_S);
    act(() => setFadeSeconds(7));
    expect(a.result.current).toBe(7);
    expect(b.result.current).toBe(7);
  });
});
