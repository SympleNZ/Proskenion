/*
 * Admin → HDMI → Inputs (§21.22, §16.1, §5.5).
 *
 * Four things are load-bearing here: naming an unclaimed physical input
 * offers only refs nothing else has claimed (the driver_ref picker, from
 * `GET /devices/{id}/refs`); editing an already-claimed one keeps its own ref
 * on offer; a delete blocked by a scene shows the reference; and a version
 * conflict reloads or overwrites like every other admin screen.
 */
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/api/client";
import { renderWithProviders } from "@/test/render";

import { HDMI_INPUTS, LKV422_REFS, MATRIX_DEVICE_ID } from "./fixtures";
import { InputsSection } from "./InputsSection";
import type { HdmiInput } from "./types";

const client = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

function mockApi(handlers: Record<string, (body?: unknown, headers?: Record<string, string>) => unknown>) {
  client.api.mockImplementation((path: string, options?: { method?: string; body?: unknown; headers?: Record<string, string> }) => {
    const method = options?.method ?? (options?.body === undefined ? "GET" : "POST");
    const handler = handlers[`${method} ${path}`];
    if (!handler) return Promise.resolve(undefined);
    try {
      return Promise.resolve(handler(options?.body, options?.headers));
    } catch (error) {
      return Promise.reject(error);
    }
  });
}

function renderSection(inputs: HdmiInput[] = HDMI_INPUTS) {
  const { container } = renderWithProviders(<InputsSection deviceId={MATRIX_DEVICE_ID} inputs={inputs} refs={LKV422_REFS} />, {
    route: "/admin/hdmi",
  });
  return container;
}

function rowFor(container: HTMLElement, ref: string): HTMLElement {
  const row = container.querySelector(`tr[data-ref="${ref}"]`);
  if (!row) throw new Error(`no row for ref ${ref}`);
  return row as HTMLElement;
}

describe("InputsSection", () => {
  beforeEach(() => {
    client.api.mockReset();
    mockApi({});
  });

  it("shows every physical ref, named or not, and offers only unclaimed refs to name", async () => {
    const container = renderSection();

    expect(within(rowFor(container, "1")).getByText("Side of stage")).toBeInTheDocument();
    expect(within(rowFor(container, "1")).getByText("✓")).toBeInTheDocument();
    expect(within(rowFor(container, "2")).getByText("Back of house")).toBeInTheDocument();
    expect(within(rowFor(container, "3")).getByText("—")).toBeInTheDocument();
    expect(within(rowFor(container, "3")).getByText("✕")).toBeInTheDocument();
    expect(within(rowFor(container, "4")).getByText("✕")).toBeInTheDocument();

    fireEvent.click(within(rowFor(container, "3")).getByRole("button", { name: "Name it" }));

    const picker = await screen.findByLabelText("Physical input");
    const options = within(picker).getAllByRole("option").map((option) => option.textContent);
    expect(options).toEqual(["Input 3", "Input 4"]); // never 1 or 2 — already claimed
    expect(picker).toHaveValue("3"); // pre-selected from the row clicked
  });

  it("creates a named input from the picked ref", async () => {
    const sent: { body?: unknown }[] = [];
    mockApi({
      "POST /hdmi/inputs": (body) => {
        sent.push({ body });
        return { id: 3, device_id: MATRIX_DEVICE_ID, driver_ref: "3", name: "Foyer feed", description: null, sort_order: 0, updated_at: "now" };
      },
    });
    const container = renderSection();

    fireEvent.click(within(rowFor(container, "3")).getByRole("button", { name: "Name it" }));
    fireEvent.change(await screen.findByLabelText("Name"), { target: { value: "Foyer feed" } });
    fireEvent.click(screen.getByRole("button", { name: "Add input" }));

    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0]?.body).toEqual({ device_id: MATRIX_DEVICE_ID, driver_ref: "3", name: "Foyer feed", description: null, sort_order: 0 });
  });

  it("edits a named input, keeping its own ref on offer", async () => {
    const sent: { body?: unknown; headers?: Record<string, string> | undefined }[] = [];
    mockApi({
      "PUT /hdmi/inputs/1": (body, headers) => {
        sent.push({ body, headers });
        return { ...HDMI_INPUTS[0], name: "Stage left" };
      },
    });
    const container = renderSection();

    fireEvent.click(within(rowFor(container, "1")).getByRole("button", { name: "Edit" }));
    const picker = await screen.findByLabelText("Physical input");
    expect(within(picker).getByRole("option", { name: "Input 1" })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Stage left" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0]?.headers).toEqual({ "If-Unmodified-Since-Version": HDMI_INPUTS[0]!.updated_at });
    expect(sent[0]?.body).toMatchObject({ driver_ref: "1", name: "Stage left" });
  });

  it("shows the scene reference on a delete blocked because it is in_use", async () => {
    mockApi({
      "DELETE /hdmi/inputs/1": () => {
        throw new ApiError(409, "in_use", "Still in use", { references: [{ entity: "scenes", id: 9, name: "Performance Start" }] });
      },
    });
    const container = renderSection();

    fireEvent.click(within(rowFor(container, "1")).getByRole("button", { name: "Delete" }));
    const confirm = await screen.findByRole("alertdialog", { name: /Delete "Side of stage"/ });
    fireEvent.click(within(confirm).getByRole("button", { name: "Delete" }));

    const guard = await screen.findByRole("alertdialog", { name: /Cannot delete/ });
    expect(within(guard).getByText("Performance Start")).toBeInTheDocument();
    expect(within(guard).getByRole("link", { name: "Performance Start" })).toHaveAttribute("href", "/admin/scenes");
  });

  it("offers reload or overwrite on a version conflict, like the other admin screens", async () => {
    const current: HdmiInput = { ...HDMI_INPUTS[0]!, name: "Renamed elsewhere", updated_at: "2026-09-10T20:00:00+12:00" };
    mockApi({
      "PUT /hdmi/inputs/1": () => {
        throw new ApiError(409, "conflict", "This was changed by someone else", { current });
      },
    });
    const container = renderSection();

    fireEvent.click(within(rowFor(container, "1")).getByRole("button", { name: "Edit" }));
    fireEvent.change(await screen.findByLabelText("Name"), { target: { value: "Mine" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    const dialog = await screen.findByRole("alertdialog");
    expect(within(dialog).getByText(/was changed by someone else/)).toBeInTheDocument();
    expect(within(dialog).getByText("Renamed elsewhere")).toBeInTheDocument();
    expect(within(dialog).getByText("Mine")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Reload theirs" })).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Overwrite with mine" })).toBeInTheDocument();
  });
});
