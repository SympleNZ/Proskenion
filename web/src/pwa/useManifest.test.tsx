/*
 * The manifest/apple-touch-icon link swap (spec §21.28, §18): both shells
 * share one `index.html`, so the correct link for each is applied at
 * runtime rather than baked into the static page.
 */
import { act, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { APPLE_TOUCH_ICON_HREF, applyManifest, MANIFEST_HREF, useManifest } from "./useManifest";

function Host({ kind }: { kind: "staff" | "hirer" }) {
  useManifest(kind);
  return null;
}

afterEach(() => {
  document.getElementById("app-manifest")?.remove();
  document.getElementById("apple-touch-icon")?.remove();
});

describe("applyManifest", () => {
  it("creates both links on first use and points them at the given kind's assets", () => {
    act(() => {
      applyManifest("hirer");
    });
    expect(document.getElementById("app-manifest")).toHaveAttribute("href", MANIFEST_HREF.hirer);
    expect(document.getElementById("apple-touch-icon")).toHaveAttribute("href", APPLE_TOUCH_ICON_HREF.hirer);
  });

  it("swaps both links to the other kind's assets, in place, rather than adding a second pair", () => {
    act(() => {
      applyManifest("staff");
    });
    act(() => {
      applyManifest("hirer");
    });
    expect(document.querySelectorAll("#app-manifest")).toHaveLength(1);
    expect(document.getElementById("app-manifest")).toHaveAttribute("href", MANIFEST_HREF.hirer);
    expect(document.getElementById("apple-touch-icon")).toHaveAttribute("href", APPLE_TOUCH_ICON_HREF.hirer);
  });

  it("the two kinds point at distinct icons (§21.28 'distinct icons')", () => {
    expect(APPLE_TOUCH_ICON_HREF.staff).not.toBe(APPLE_TOUCH_ICON_HREF.hirer);
    expect(MANIFEST_HREF.staff).not.toBe(MANIFEST_HREF.hirer);
  });
});

describe("useManifest", () => {
  it("applies the given kind's links on mount", () => {
    render(<Host kind="hirer" />);
    expect(document.getElementById("app-manifest")).toHaveAttribute("href", "/manifest.hirer.json");
    expect(document.getElementById("apple-touch-icon")).toHaveAttribute("href", APPLE_TOUCH_ICON_HREF.hirer);
  });
});
