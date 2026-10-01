/*
 * A page's group master item (spec §21.9 "Group trays"): the tray renders
 * only when the server answers `tray: true`; the collapse toggle is
 * session-only, changes no server state, and needs no DOM measurement; and
 * the collapse's motion is driven by design tokens, collapsing globally
 * under `prefers-reduced-motion` (base.css).
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { LightingChannel } from "@/lighting/types";
import { resetLiveState, setLevel } from "@/live/store";

vi.mock("@/live/socket", () => ({ send: vi.fn() }));

import { send } from "@/live/socket";

import { PageGroupItem } from "./PageGroupItem";
import type { PageGroupMasterItem } from "./types";

function member(id: number, name: string): LightingChannel {
  return {
    id,
    name,
    type: "dmx",
    min_value: 0,
    max_value: 100,
    has_colour: false,
    group_ids: [1],
    bar_id: null,
    position: null,
    visible_staff: true,
    updated_at: "2026-09-04T14:30:00+12:00",
  };
}

const CHANNELS_BY_ID = new Map<number, LightingChannel>([
  [10, member(10, "Fixture 10")],
  [11, member(11, "Fixture 11")],
  [12, member(12, "Fixture 12")],
]);

function groupMasterItem(overrides: Partial<PageGroupMasterItem> = {}): PageGroupMasterItem {
  return {
    id: 12,
    sort_order: 2,
    kind: "group_master",
    group_id: 1,
    expanded: false,
    // A server-supplied colour string, deliberately not shaped like a real
    // CSS colour value so it cannot be mistaken for a literal one in this
    // source file (token discipline forbids one outside tokens.css).
    group: { id: 1, name: "Row 1", colour: "amber-group", sort_order: 0, channel_ids: [10, 11, 12], updated_at: "" },
    members: [10, 11, 12],
    tray: true,
    ...overrides,
  };
}

beforeEach(() => {
  resetLiveState();
  vi.mocked(send).mockClear();
});

describe("PageGroupItem — tray render vs tray: false (§21.9 contiguity)", () => {
  it("renders a tray with every member's own strip when tray is true", () => {
    render(<PageGroupItem item={groupMasterItem({ expanded: true })} channelsById={CHANNELS_BY_ID} />);

    expect(screen.getByText("Row 1", { selector: ".page-group-tray-name" })).toBeInTheDocument();
    expect(screen.getByRole("slider", { name: "Fixture 10 fader" })).toBeInTheDocument();
    expect(screen.getByRole("slider", { name: "Fixture 11 fader" })).toBeInTheDocument();
    expect(screen.getByRole("slider", { name: "Fixture 12 fader" })).toBeInTheDocument();
    expect(screen.getAllByRole("slider", { name: "Row 1 fader" })).toHaveLength(1); // the master
    expect(screen.queryByText(/^3 fixtures$/)).not.toBeInTheDocument(); // that marker is the tray:false rendering only
  });

  it("renders an ordinary strip with a member-count marker, and no member strips, when tray is false", () => {
    render(<PageGroupItem item={groupMasterItem({ tray: false })} channelsById={CHANNELS_BY_ID} />);

    expect(screen.getByRole("slider", { name: "Row 1 fader" })).toBeInTheDocument();
    expect(screen.getByText("3 fixtures")).toBeInTheDocument();
    expect(screen.queryByRole("slider", { name: "Fixture 10 fader" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /collapse|fixtures/i })).not.toBeInTheDocument(); // no toggle either
  });
});

describe("PageGroupItem — the master sets and shows its members' levels (owner decision 2026-09-30)", () => {
  it("shows the highest member level marked mixed, and sends the group level", () => {
    setLevel(10, 40);
    setLevel(11, 75);
    setLevel(12, 40);
    render(<PageGroupItem item={groupMasterItem({ tray: false })} channelsById={CHANNELS_BY_ID} />);
    const master = screen.getByRole("slider", { name: "Row 1 fader" });
    expect(master).toHaveAttribute("aria-valuetext", "75.0%");
    expect(screen.getByText("mixed")).toBeInTheDocument();
    fireEvent.keyDown(master, { key: "Home" });
    expect(send).toHaveBeenLastCalledWith("lighting_group", 1, 0);
  });
});

describe("PageGroupItem — the master's BUMP (owner decision 2026-10-01)", () => {
  it.each([true, false])("offers a BUMP under the master, tray %s", (tray) => {
    render(<PageGroupItem item={groupMasterItem({ tray })} channelsById={CHANNELS_BY_ID} />);
    const bump = screen.getByRole("button", { name: "Bump Row 1 to full" });
    expect(bump).toHaveAttribute("aria-pressed", "false");
    expect(bump).toBeEnabled();
  });

  it("offers none on an indicator-only group", () => {
    const item = groupMasterItem();
    render(<PageGroupItem item={{ ...item, group: { ...item.group, indicator_only: true } }} channelsById={CHANNELS_BY_ID} />);
    expect(screen.queryByRole("button", { name: /bump/i })).not.toBeInTheDocument();
  });
});

describe("PageGroupItem — the collapse toggle (§21.9 'the toggle lasts the session')", () => {
  it("opens at the page's stored state and flips locally without writing to the store or the server", () => {
    render(<PageGroupItem item={groupMasterItem({ expanded: false })} channelsById={CHANNELS_BY_ID} />);
    const tray = screen.getByTestId("page-group-12");
    expect(tray).toHaveAttribute("data-expanded", "false");

    const toggle = screen.getByRole("button", { name: /3 fixtures/i });
    fireEvent.click(toggle);

    expect(tray).toHaveAttribute("data-expanded", "true");
    expect(within(tray).getByRole("button", { name: /collapse/i })).toBeInTheDocument();
    // Nothing about a tray's opening state is a write (§21.9: "the toggle
    // lasts the session without rewriting the layout").
    expect(send).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: /collapse/i }));
    expect(tray).toHaveAttribute("data-expanded", "false");
    expect(send).not.toHaveBeenCalled();
  });

  it("needs no DOM measurement: the expanded width comes from a member-count custom property, set once, not recomputed on toggle", () => {
    const rectSpy = vi.spyOn(HTMLElement.prototype, "getBoundingClientRect");
    render(<PageGroupItem item={groupMasterItem({ expanded: false })} channelsById={CHANNELS_BY_ID} />);
    // `--page-tray-count` is set on the tray itself, and `.page-group-tray-members`
    // reads it through ordinary CSS custom-property inheritance — never
    // recomputed by the toggle, which only ever flips `data-expanded`.
    const tray = screen.getByTestId("page-group-12");
    expect(tray.style.getPropertyValue("--page-tray-count")).toBe("3");

    rectSpy.mockClear();
    fireEvent.click(screen.getByRole("button", { name: /3 fixtures/i }));

    expect(tray).toHaveAttribute("data-expanded", "true");
    // The count carried to CSS is arithmetic, not a measured pixel value —
    // unchanged by the toggle, and no layout measurement API was consulted.
    expect(tray.style.getPropertyValue("--page-tray-count")).toBe("3");
    expect(rectSpy).not.toHaveBeenCalled();
    rectSpy.mockRestore();
  });
});

describe("PageGroupItem — members_writable (§18 Q3, P5-T11)", () => {
  it("renders members read-only, master unaffected, when members_writable is false", () => {
    render(<PageGroupItem item={groupMasterItem({ expanded: true, members_writable: false })} channelsById={CHANNELS_BY_ID} />);

    expect(screen.getByRole("slider", { name: "Fixture 10 fader" })).toHaveAttribute("aria-readonly", "true");
    expect(screen.getByRole("slider", { name: "Fixture 11 fader" })).toHaveAttribute("aria-readonly", "true");
    expect(screen.getByRole("slider", { name: "Fixture 12 fader" })).toHaveAttribute("aria-readonly", "true");
    // Only the master moves (§18 Q3) — it never gets the members' restriction.
    expect(screen.getByRole("slider", { name: "Row 1 fader" })).not.toHaveAttribute("aria-readonly");
  });

  it("leaves members writable when members_writable is absent (an operator's own page)", () => {
    render(<PageGroupItem item={groupMasterItem({ expanded: true })} channelsById={CHANNELS_BY_ID} />);
    expect(screen.getByRole("slider", { name: "Fixture 10 fader" })).not.toHaveAttribute("aria-readonly");
  });

  it("leaves members writable when members_writable is explicitly true", () => {
    render(<PageGroupItem item={groupMasterItem({ expanded: true, members_writable: true })} channelsById={CHANNELS_BY_ID} />);
    expect(screen.getByRole("slider", { name: "Fixture 10 fader" })).not.toHaveAttribute("aria-readonly");
  });
});

describe("PageGroupItem — reduced motion (§21.3, §21.9)", () => {
  const here = dirname(fileURLToPath(import.meta.url));
  const componentsCss = readFileSync(join(here, "..", "styles", "components.css"), "utf8");
  const baseCss = readFileSync(join(here, "..", "styles", "base.css"), "utf8");

  it("drives the tray's collapse from design tokens, never a literal duration", () => {
    const block = componentsCss.slice(componentsCss.indexOf(".page-group-tray-members {"), componentsCss.indexOf(".page-group-tray[data-expanded"));
    expect(block).toMatch(/width\s+var\(--duration-moderate\)/);
    expect(block).toMatch(/opacity\s+var\(--duration-base\)/);
  });

  it("the global reduced-motion rule collapses every transition duration to --duration-reduced, covering the tray", () => {
    expect(baseCss).toMatch(/@media \(prefers-reduced-motion: reduce\)/);
    expect(baseCss).toMatch(/transition-duration:\s*var\(--duration-reduced\)\s*!important/);
  });
});
