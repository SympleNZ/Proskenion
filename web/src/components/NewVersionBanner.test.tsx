/*
 * "A new version is installed — Refresh" (spec §16 "Service worker"): shown
 * once, never as an auto-reload — the operator presses Refresh.
 */
import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { resetNewVersionForTests } from "@/version/versionCheck";

import { NewVersionBanner } from "./NewVersionBanner";

beforeEach(() => {
  resetNewVersionForTests();
});

afterEach(() => {
  resetNewVersionForTests();
});

describe("NewVersionBanner", () => {
  it("renders nothing until a new version has been noted", () => {
    const { container } = render(<NewVersionBanner />);
    expect(container).toBeEmptyDOMElement();
  });

  it("shows the nudge, and Refresh calls the reload handler rather than auto-reloading", async () => {
    const { checkVersion } = await import("@/version/versionCheck");
    const onReload = vi.fn();
    render(<NewVersionBanner onReload={onReload} />);

    await act(async () => {
      await checkVersion(
        vi.fn(async () => ({ ok: true, json: async () => ({ version: "9.9.9" }) }) as unknown as Response) as unknown as typeof fetch,
      );
    });

    expect(screen.getByText("A new version is installed")).toBeInTheDocument();
    expect(onReload).not.toHaveBeenCalled(); // never an auto-reload underneath someone

    screen.getByRole("button", { name: "Refresh" }).click();
    expect(onReload).toHaveBeenCalledTimes(1);
  });
});
