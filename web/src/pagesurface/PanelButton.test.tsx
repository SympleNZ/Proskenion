/*
 * One panel button (spec §21.9): the LED anchored top with the label
 * centred in what remains, the lamp — never the fire response — deciding
 * the lit/transitioning state, `confirm`, "a scroll is not a tap", and that
 * firing calls the contract's own endpoint.
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { fireEvent, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { resetLiveState, setLamp } from "@/live/store";
import { renderWithProviders } from "@/test/render";

const client = vi.hoisted(() => ({ api: vi.fn() }));
vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

const toastSpy = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn(), warning: vi.fn() }));
vi.mock("sonner", () => ({ toast: toastSpy }));

import { PanelButton } from "./PanelButton";
import type { PanelButtonSpec } from "./types";

function spec(overrides: Partial<PanelButtonSpec> = {}): PanelButtonSpec {
  return {
    id: 40,
    col: 0,
    row: 0,
    label: "House up",
    rule_id: 9,
    state_id: 4,
    colour: "amber",
    confirm: false,
    devices: [],
    ...overrides,
  };
}

beforeEach(() => {
  resetLiveState();
  client.api.mockReset();
  client.api.mockResolvedValue({ rule_id: 9, triggered_by: "page:40", fired: true, guard_result: null, result: "started", detail: {} });
  toastSpy.success.mockClear();
  toastSpy.error.mockClear();
  toastSpy.warning.mockClear();
});

describe("PanelButton — structure (§21.9 'LED anchored to the top')", () => {
  it("renders the LED before the label, as direct children in that order", () => {
    renderWithProviders(<PanelButton pageId={1} spec={spec()} side={126} />);
    const button = screen.getByTestId("panel-button-40");
    const children = [...button.children];
    expect(children[0]).toHaveClass("panel-button-led");
    expect(children[1]).toHaveClass("panel-button-label");
    expect(children[1]).toHaveTextContent("House up");
  });

  it("is a real, keyboard-operable button with an accessible name that is never colour alone", () => {
    renderWithProviders(<PanelButton pageId={1} spec={spec()} side={126} />);
    const button = screen.getByRole("button", { name: "House up, off" });
    expect(button.tagName).toBe("BUTTON");
  });
});

describe("PanelButton — the lamp decides the lit state, never the fire response (§21.9, §8.6)", () => {
  it("renders off when no status frame has said anything yet", () => {
    renderWithProviders(<PanelButton pageId={1} spec={spec({ state_id: 5 })} side={126} />);
    expect(screen.getByTestId("panel-button-40")).toHaveAttribute("data-lamp", "off");
  });

  it("latches when its lamp is on", () => {
    setLamp(5, { on: true, transitioning: false });
    renderWithProviders(<PanelButton pageId={1} spec={spec({ state_id: 5 })} side={126} />);
    expect(screen.getByRole("button", { name: "House up, on" })).toHaveAttribute("data-lamp", "on");
  });

  it("shows the amber transitioning state, which overrides an on lamp", () => {
    setLamp(5, { on: true, transitioning: true });
    renderWithProviders(<PanelButton pageId={1} spec={spec({ state_id: 5 })} side={126} />);
    expect(screen.getByRole("button", { name: "House up, transitioning" })).toHaveAttribute("data-lamp", "transitioning");
  });

  it("a button with no state_id never latches, whatever else is in the store", () => {
    setLamp(4, { on: true, transitioning: false }); // id 4 happens to be a real state elsewhere
    renderWithProviders(<PanelButton pageId={1} spec={spec({ state_id: null })} side={126} />);
    expect(screen.getByTestId("panel-button-40")).toHaveAttribute("data-lamp", "off");
  });
});

describe("PanelButton — firing (§18 contract: POST /pages/{id}/buttons/{bid})", () => {
  it("fires the endpoint for its own page and button id on a plain tap", async () => {
    renderWithProviders(<PanelButton pageId={7} spec={spec({ confirm: false })} side={126} />);
    fireEvent.click(screen.getByTestId("panel-button-40"));
    await waitFor(() => expect(client.api).toHaveBeenCalledWith("/pages/7/buttons/40", expect.objectContaining({ method: "POST" })));
  });

  it("confirm: true shows a dialog before firing, and does not fire on cancel", async () => {
    renderWithProviders(<PanelButton pageId={7} spec={spec({ confirm: true })} side={126} />);
    fireEvent.click(screen.getByTestId("panel-button-40"));

    expect(await screen.findByRole("alertdialog")).toBeInTheDocument();
    expect(client.api).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(client.api).not.toHaveBeenCalled();
  });

  it("confirm: true fires once the dialog is confirmed", async () => {
    renderWithProviders(<PanelButton pageId={7} spec={spec({ confirm: true })} side={126} />);
    fireEvent.click(screen.getByTestId("panel-button-40"));
    fireEvent.click(await screen.findByRole("button", { name: "Fire" }));
    await waitFor(() => expect(client.api).toHaveBeenCalledWith("/pages/7/buttons/40", expect.objectContaining({ method: "POST" })));
  });
});

describe("PanelButton — a scroll is not a tap (CONVENTIONS)", () => {
  it("does not fire when the pointer moves past the threshold before release", () => {
    renderWithProviders(<PanelButton pageId={7} spec={spec()} side={126} />);
    const button = screen.getByTestId("panel-button-40");
    fireEvent.pointerDown(button, { pointerId: 1, clientX: 10, clientY: 10 });
    fireEvent.pointerMove(button, { pointerId: 1, clientX: 60, clientY: 10 });
    fireEvent.pointerUp(button, { pointerId: 1, clientX: 60, clientY: 10 });
    fireEvent.click(button);
    expect(client.api).not.toHaveBeenCalled();
  });

  it("still fires a tap that stays within the threshold", async () => {
    renderWithProviders(<PanelButton pageId={7} spec={spec()} side={126} />);
    const button = screen.getByTestId("panel-button-40");
    fireEvent.pointerDown(button, { pointerId: 1, clientX: 10, clientY: 10 });
    fireEvent.pointerMove(button, { pointerId: 1, clientX: 13, clientY: 10 });
    fireEvent.pointerUp(button, { pointerId: 1, clientX: 13, clientY: 10 });
    fireEvent.click(button);
    await waitFor(() => expect(client.api).toHaveBeenCalled());
  });
});

describe("PanelButton — plain hirer notifications (§21.15, §24.6)", () => {
  it("shows the operator's own detailed wording by default", async () => {
    client.api.mockResolvedValue({ rule_id: 9, triggered_by: "page:40", fired: false, guard_result: "blocked", result: "blocked", detail: {} });
    renderWithProviders(<PanelButton pageId={7} spec={spec()} side={126} />);
    fireEvent.click(screen.getByTestId("panel-button-40"));
    await waitFor(() => expect(toastSpy.warning).toHaveBeenCalledWith("House up: blocked by the guard"));
  });

  it('says only "Done" on a hirer success, never the button label or the result code', async () => {
    renderWithProviders(<PanelButton pageId={7} spec={spec()} side={126} hirer />);
    fireEvent.click(screen.getByTestId("panel-button-40"));
    await waitFor(() => expect(toastSpy.success).toHaveBeenCalledWith("Done"));
    expect(toastSpy.success).not.toHaveBeenCalledWith("House up");
  });

  it("says only the one blanket failure message on a hirer's blocked or unfired button, never the guard or result", async () => {
    client.api.mockResolvedValue({ rule_id: 9, triggered_by: "page:40", fired: false, guard_result: "blocked", result: "blocked", detail: {} });
    renderWithProviders(<PanelButton pageId={7} spec={spec()} side={126} hirer />);
    fireEvent.click(screen.getByTestId("panel-button-40"));
    await waitFor(() => expect(toastSpy.error).toHaveBeenCalledWith("Something went wrong — please speak to venue staff"));
    expect(toastSpy.warning).not.toHaveBeenCalled();
  });

  it("says the same one blanket message on a hirer's request failure, never the technical error", async () => {
    client.api.mockRejectedValue(new Error("network down"));
    renderWithProviders(<PanelButton pageId={7} spec={spec()} side={126} hirer />);
    fireEvent.click(screen.getByTestId("panel-button-40"));
    await waitFor(() => expect(toastSpy.error).toHaveBeenCalledWith("Something went wrong — please speak to venue staff"));
  });
});

describe("PanelButton — reduced motion (§21.3, §24.5)", () => {
  const here = dirname(fileURLToPath(import.meta.url));
  const baseCss = readFileSync(join(here, "..", "styles", "base.css"), "utf8");

  it("stops the transitioning lamp's amber pulse and keeps the static glow", () => {
    expect(baseCss).toMatch(/@media \(prefers-reduced-motion: reduce\)/);
    const block = baseCss.slice(baseCss.indexOf('.panel-button[data-lamp="transitioning"]'));
    expect(block).toMatch(/animation:\s*none/);
    expect(block).toMatch(/box-shadow:\s*0 0 var\(--led-glow-blur\) var\(--led-glow-spread\) var\(--color-warning\)/);
  });
});
