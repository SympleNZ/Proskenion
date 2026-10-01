/*
 * `usePhoneLandscape` (the owner's decision, 2026-09 — see the module's own
 * doc comment): landscape AND short AND a coarse pointer, all three.
 *
 * jsdom has no `matchMedia` and no layout engine, so a mock that just hands
 * back a fixed `matches` would only prove the hook reads whatever it is
 * told — not that the query itself carries the right three conditions. This
 * fake evaluates the query's own `orientation`/`max-height`/`pointer`
 * features against a test-controlled environment instead, so the negative
 * cases (a tablet in landscape, a short desktop window) exercise the same
 * text a real browser would parse.
 */
import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { PHONE_LANDSCAPE_MAX_HEIGHT_PX, PHONE_LANDSCAPE_QUERY, usePhoneLandscape } from "./phoneLandscape";

interface Env {
  orientation: "landscape" | "portrait";
  heightPx: number;
  pointer: "coarse" | "fine";
}

type Listener = () => void;

let env: Env = { orientation: "portrait", heightPx: 844, pointer: "coarse" };
const listeners = new Set<Listener>();

function evaluate(query: string, e: Env): boolean {
  const orientation = /orientation:\s*(\w+)/.exec(query)?.[1];
  const maxHeight = /max-height:\s*(\d+)px/.exec(query)?.[1];
  const pointer = /pointer:\s*(\w+)/.exec(query)?.[1];
  if (orientation !== undefined && orientation !== e.orientation) return false;
  if (maxHeight !== undefined && e.heightPx > Number(maxHeight)) return false;
  if (pointer !== undefined && pointer !== e.pointer) return false;
  return true;
}

function installFakeMatchMedia(): void {
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    get matches() {
      return evaluate(query, env);
    },
    media: query,
    onchange: null,
    addEventListener: (_type: string, cb: Listener) => listeners.add(cb),
    removeEventListener: (_type: string, cb: Listener) => listeners.delete(cb),
    addListener: (cb: Listener) => listeners.add(cb),
    removeListener: (cb: Listener) => listeners.delete(cb),
    dispatchEvent: () => true,
  }));
}

function setEnv(next: Partial<Env>): void {
  env = { ...env, ...next };
  for (const listener of listeners) listener();
}

beforeEach(() => {
  env = { orientation: "portrait", heightPx: 844, pointer: "coarse" };
  listeners.clear();
  installFakeMatchMedia();
});

afterEach(() => {
  listeners.clear();
  // @ts-expect-error — jsdom has no matchMedia of its own to restore to.
  delete window.matchMedia;
});

describe("PHONE_LANDSCAPE_QUERY — all three conditions, one query", () => {
  it("carries orientation, the short-height threshold and a coarse pointer", () => {
    expect(PHONE_LANDSCAPE_QUERY).toContain("orientation: landscape");
    expect(PHONE_LANDSCAPE_QUERY).toContain(`max-height: ${PHONE_LANDSCAPE_MAX_HEIGHT_PX}px`);
    expect(PHONE_LANDSCAPE_QUERY).toContain("pointer: coarse");
  });
});

describe("usePhoneLandscape", () => {
  it("is false in portrait, even short and coarse — a portrait phone is never blocked", () => {
    setEnv({ orientation: "portrait", heightPx: 390, pointer: "coarse" });
    const { result } = renderHook(() => usePhoneLandscape());
    expect(result.current).toBe(false);
  });

  it("is true for a phone rotated to landscape — coarse, short, landscape", () => {
    setEnv({ orientation: "landscape", heightPx: 390, pointer: "coarse" });
    const { result } = renderHook(() => usePhoneLandscape());
    expect(result.current).toBe(true);
  });

  it("is false for a tablet in landscape — the smallest supported one (834 tall) clears the threshold", () => {
    setEnv({ orientation: "landscape", heightPx: 834, pointer: "coarse" });
    const { result } = renderHook(() => usePhoneLandscape());
    expect(result.current).toBe(false);
  });

  it("is false for a short, narrow desktop window — a fine pointer is never blocked", () => {
    setEnv({ orientation: "landscape", heightPx: 390, pointer: "fine" });
    const { result } = renderHook(() => usePhoneLandscape());
    expect(result.current).toBe(false);
  });

  it("re-evaluates live on rotation, without remounting", () => {
    setEnv({ orientation: "portrait", heightPx: 844, pointer: "coarse" });
    const { result } = renderHook(() => usePhoneLandscape());
    expect(result.current).toBe(false);

    act(() => setEnv({ orientation: "landscape", heightPx: 390 }));
    expect(result.current).toBe(true);

    act(() => setEnv({ orientation: "portrait", heightPx: 844 }));
    expect(result.current).toBe(false);
  });

  it("is false where matchMedia does not exist (a defensive fallback, never the render path)", () => {
    // @ts-expect-error — simulating an environment with no matchMedia at all.
    delete window.matchMedia;
    const { result } = renderHook(() => usePhoneLandscape());
    expect(result.current).toBe(false);
  });
});
