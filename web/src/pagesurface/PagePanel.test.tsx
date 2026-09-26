/*
 * A page's button panel (spec §21.9): `row`/`col` are a row-major reading
 * order (phase-5-contracts.md, Q5) regardless of the order buttons arrive
 * in, and unfilled trailing cells render dashed and empty rather than being
 * silently dropped.
 */
import { screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { resetLiveState } from "@/live/store";
import { renderWithProviders } from "@/test/render";

import { PagePanel } from "./PagePanel";
import type { PagePanelItem, PanelButtonSpec } from "./types";

function button(id: number, label: string, row: number, col: number, overrides: Partial<PanelButtonSpec> = {}): PanelButtonSpec {
  return { id, col, row, label, rule_id: id, state_id: null, colour: null, confirm: false, devices: [], ...overrides };
}

function panelItem(buttons: readonly PanelButtonSpec[], panelWidth = 2): PagePanelItem {
  return { id: 13, sort_order: 3, kind: "panel", panel_title: "Room", panel_width: panelWidth, buttons };
}

beforeEach(() => {
  resetLiveState();
});

describe("PagePanel — row-major reading order (§21.9, Q5)", () => {
  it("lays buttons out by (row, col) regardless of the order they arrive in the array", () => {
    // 2 columns, 5 buttons → 3 rows, cell (2,1) unfilled. Scrambled on purpose.
    const buttons = [
      button(105, "E", 2, 0),
      button(103, "C", 1, 0),
      button(101, "A", 0, 0),
      button(104, "D", 1, 1),
      button(102, "B", 0, 1),
    ];
    renderWithProviders(<PagePanel pageId={1} item={panelItem(buttons)} surfaceWidth={0} />);

    const grid = screen.getByTestId("page-panel-13").querySelector(".panel-grid");
    expect(grid).not.toBeNull();
    const cellLabels = [...(grid as HTMLElement).children].map((cell) =>
      cell.classList.contains("panel-empty-cell") ? null : cell.textContent,
    );
    expect(cellLabels).toEqual(["A", "B", "C", "D", "E", null]);
  });

  it("renders a dashed, hidden placeholder for every unused position", () => {
    // 2 columns configured, only 1 button: one filled cell, one empty one.
    const buttons = [button(101, "A", 0, 0)];
    renderWithProviders(<PagePanel pageId={1} item={panelItem(buttons, 2)} surfaceWidth={0} />);
    const grid = screen.getByTestId("page-panel-13").querySelector(".panel-grid") as HTMLElement;
    const empties = grid.querySelectorAll(".panel-empty-cell");
    expect(empties.length).toBeGreaterThan(0);
    for (const empty of empties) {
      expect(empty).toHaveAttribute("aria-hidden", "true");
    }
  });

  it("never shows fewer columns than configured, even for a panel with only one button", () => {
    const buttons = [button(101, "A", 0, 0)];
    renderWithProviders(<PagePanel pageId={1} item={panelItem(buttons, 3)} surfaceWidth={0} />);
    const grid = screen.getByTestId("page-panel-13").querySelector(".panel-grid") as HTMLElement;
    // repeat(3, ...) or wider — never repeat(1, …) or repeat(2, …).
    expect(grid.style.gridTemplateColumns).not.toMatch(/^repeat\(1,/);
    expect(grid.style.gridTemplateColumns).not.toMatch(/^repeat\(2,/);
  });
});

describe("PagePanel — the title", () => {
  it("shows the configured panel title", () => {
    const buttons = [button(101, "A", 0, 0)];
    renderWithProviders(<PagePanel pageId={1} item={panelItem(buttons)} surfaceWidth={0} />);
    expect(screen.getByText("Room", { selector: ".panel-title" })).toBeInTheDocument();
  });
});
