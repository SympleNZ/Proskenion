/*
 * `AppRoot` — the wiring between `usePhoneLandscape` and `#app-root`'s
 * `inert`/`aria-hidden` (spec-free, the owner's decision;
 * `lib/phoneLandscape.ts`'s doc comment has the detection rule). The guard
 * overlay itself sits beside `#app-root`, never inside it, so it is never
 * the thing being hidden.
 */
import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AppRoot } from "./AppRoot";

let matches = false;
const listeners = new Set<() => void>();

beforeEach(() => {
  matches = false;
  listeners.clear();
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    get matches() {
      return matches;
    },
    media: query,
    onchange: null,
    addEventListener: (_type: string, cb: () => void) => listeners.add(cb),
    removeEventListener: (_type: string, cb: () => void) => listeners.delete(cb),
    addListener: (cb: () => void) => listeners.add(cb),
    removeListener: (cb: () => void) => listeners.delete(cb),
    dispatchEvent: () => true,
  }));
});

afterEach(() => {
  listeners.clear();
  // @ts-expect-error — jsdom has no matchMedia of its own to restore to.
  delete window.matchMedia;
});

function setMatches(next: boolean): void {
  matches = next;
  for (const listener of listeners) listener();
}

describe("AppRoot", () => {
  it("leaves #app-root reachable in portrait, and renders the guard beside it either way", () => {
    render(
      <AppRoot>
        <p>the app</p>
      </AppRoot>,
    );
    const appRoot = document.getElementById("app-root");
    expect(appRoot).not.toBeNull();
    expect(appRoot).not.toHaveAttribute("inert");
    expect(appRoot).not.toHaveAttribute("aria-hidden");
    expect(screen.getByText("the app")).toBeInTheDocument();
    // The guard is always mounted — its own visibility is CSS, not this.
    expect(screen.getByRole("alertdialog", { hidden: true })).toBeInTheDocument();
  });

  it("inerts and aria-hides #app-root while a phone is in landscape, without unmounting the app", () => {
    setMatches(true);
    render(
      <AppRoot>
        <p>the app</p>
      </AppRoot>,
    );
    const appRoot = document.getElementById("app-root");
    expect(appRoot).toHaveAttribute("inert");
    expect(appRoot).toHaveAttribute("aria-hidden", "true");
    // Still in the DOM — nothing unmounted, only hidden from assistive tech.
    expect(screen.getByText("the app")).toBeInTheDocument();
  });

  it("restores #app-root on rotation back to portrait", () => {
    setMatches(true);
    render(
      <AppRoot>
        <p>the app</p>
      </AppRoot>,
    );
    expect(document.getElementById("app-root")).toHaveAttribute("inert");

    act(() => setMatches(false));
    expect(document.getElementById("app-root")).not.toHaveAttribute("inert");
    expect(document.getElementById("app-root")).not.toHaveAttribute("aria-hidden");
  });
});
