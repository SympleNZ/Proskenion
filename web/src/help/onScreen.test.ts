/*
 * `onScreen.ts` (spec §21.24 *Help*, Simon's 28 Sep "On this screen"
 * request): the DOM-derived entry collector, the route resolver, and the
 * coverage check that every screen `navigation.ts` declares has a summary —
 * see `OnScreenSection.test.tsx` for the rendered-component behaviour.
 */
import { describe, expect, it } from "vitest";

import { ADMIN_ITEMS, OPERATOR_TABS } from "@/navigation";

import {
  ADMIN_SCREEN_HELP,
  collectOnScreenEntries,
  HIRER_SCREEN_HELP,
  OPERATOR_SCREEN_HELP,
  resolveScreen,
} from "./onScreen";

describe("collectOnScreenEntries", () => {
  it("finds every [data-help-trigger] under root, in document order", () => {
    const root = document.createElement("div");
    root.innerHTML = `
      <div class="field">
        <button data-help-trigger data-help-id="devices.category"></button>
      </div>
      <button data-help-trigger data-help-id="devices.driver"></button>
    `;
    const entries = collectOnScreenEntries(root);
    expect(entries.map((e) => e.id)).toEqual(["devices.category", "devices.driver"]);
    expect(entries[0]).toMatchObject({ term: "What is it" });
  });

  it("returns nothing for a root with no help triggers", () => {
    const root = document.createElement("div");
    root.innerHTML = "<p>Nothing here.</p>";
    expect(collectOnScreenEntries(root)).toEqual([]);
  });

  it("deduplicates a repeated id, keeping the first", () => {
    const root = document.createElement("div");
    root.innerHTML = `
      <button data-help-trigger data-help-id="devices.category"></button>
      <button data-help-trigger data-help-id="devices.category"></button>
    `;
    expect(collectOnScreenEntries(root)).toHaveLength(1);
  });

  it("ignores an id that is not a real help entry — an attribute is just a string, not a guarantee", () => {
    const root = document.createElement("div");
    root.innerHTML = `<button data-help-trigger data-help-id="not-a-real-id"></button>`;
    expect(collectOnScreenEntries(root)).toEqual([]);
  });
});

describe("resolveScreen", () => {
  it("resolves an admin path for an admin", () => {
    expect(resolveScreen("admin", "/admin/devices")?.help).toBe(ADMIN_SCREEN_HELP["devices"]);
  });

  it("resolves an operator path for an operator, and for an admin previewing it", () => {
    expect(resolveScreen("operator", "/app/mixer")?.help).toBe(OPERATOR_SCREEN_HELP["mixer"]);
    expect(resolveScreen("admin", "/app/mixer")?.help).toBe(OPERATOR_SCREEN_HELP["mixer"]);
  });

  it("resolves any /hire path for a hirer, to the one fixed hirer screen", () => {
    expect(resolveScreen("hirer", "/hire/42")?.help).toBe(HIRER_SCREEN_HELP);
    expect(resolveScreen("hirer", "/hire")?.help).toBe(HIRER_SCREEN_HELP);
  });

  it("never resolves an admin path for an operator or a hirer", () => {
    expect(resolveScreen("operator", "/admin/devices")).toBeNull();
    expect(resolveScreen("hirer", "/admin/devices")).toBeNull();
  });

  it("never resolves an operator path for a hirer", () => {
    expect(resolveScreen("hirer", "/app/pages")).toBeNull();
  });

  it("never resolves a /hire path for an admin or an operator", () => {
    expect(resolveScreen("admin", "/hire/1")).toBeNull();
    expect(resolveScreen("operator", "/hire/1")).toBeNull();
  });

  it("resolves nothing for /login, /setup, or an unknown admin segment", () => {
    expect(resolveScreen("admin", "/login")).toBeNull();
    expect(resolveScreen("admin", "/setup")).toBeNull();
    expect(resolveScreen("admin", "/admin/not-a-real-screen")).toBeNull();
  });
});

describe("screen summary coverage", () => {
  // The negative case: this must fail the moment a screen is added to
  // navigation.ts without a matching summary here — proved while writing
  // this by deleting one entry above and watching the assertion below fail.
  it("every admin nav item in navigation.ts has a summary", () => {
    const missing = ADMIN_ITEMS.filter((item) => !ADMIN_SCREEN_HELP[item.path]).map((item) => item.path);
    expect(missing).toEqual([]);
  });

  it("every operator tab in navigation.ts has a summary", () => {
    const missing = OPERATOR_TABS.filter((item) => !OPERATOR_SCREEN_HELP[item.path]).map((item) => item.path);
    expect(missing).toEqual([]);
  });

  it("has no summary for a path navigation.ts does not declare — the map only ever grows to match it", () => {
    const adminPaths = new Set(ADMIN_ITEMS.map((item) => item.path));
    const operatorPaths = new Set(OPERATOR_TABS.map((item) => item.path));
    expect(Object.keys(ADMIN_SCREEN_HELP).filter((path) => !adminPaths.has(path))).toEqual([]);
    expect(Object.keys(OPERATOR_SCREEN_HELP).filter((path) => !operatorPaths.has(path))).toEqual([]);
  });
});
