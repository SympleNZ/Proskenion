/*
 * Admin → HDMI → Destinations (§21.22, §7.5, §15.10, §13.5, §16.1).
 *
 * Load-bearing: the outputs list is ordered and the first is marked
 * authoritative for display; the default input is what "Restore Venue
 * Default" would set (§13.5); a delete blocked by a scene shows the
 * reference; and a version conflict reloads or overwrites like every other
 * admin screen.
 */
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/api/client";
import { renderWithProviders } from "@/test/render";

import { DestinationsSection } from "./DestinationsSection";
import { HDMI_DESTINATIONS, HDMI_INPUTS, HDMI_OUTPUTS, MATRIX_DEVICE_ID } from "./fixtures";
import type { HdmiDestination } from "./types";

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

function renderSection(destinations: HdmiDestination[] = HDMI_DESTINATIONS) {
  return renderWithProviders(
    <DestinationsSection deviceId={MATRIX_DEVICE_ID} destinations={destinations} outputs={HDMI_OUTPUTS} inputs={HDMI_INPUTS} supportsAtomicRoute />,
    { route: "/admin/hdmi" },
  );
}

describe("DestinationsSection", () => {
  beforeEach(() => {
    client.api.mockReset();
    mockApi({});
  });

  it("orders the outputs and marks the first authoritative for display", () => {
    renderSection();
    const row = screen.getByText("The room").closest("tr") as HTMLElement;
    expect(within(row).getByText("Main Projector (display), BOH Return")).toBeInTheDocument();
  });

  it("shows the default input's name, and 'Not set' when there is none", () => {
    const noDefault: HdmiDestination = { ...HDMI_DESTINATIONS[0]!, id: 2, name: "Foyer", default_input_id: null };
    renderSection([HDMI_DESTINATIONS[0]!, noDefault]);

    const room = screen.getByText("The room").closest("tr") as HTMLElement;
    expect(within(room).getByText("Side of stage")).toBeInTheDocument();
    const foyer = screen.getByText("Foyer").closest("tr") as HTMLElement;
    expect(within(foyer).getByText("Not set")).toBeInTheDocument();
  });

  it("orders outputs by selection and lets the operator reorder before saving", async () => {
    const sent: { body?: unknown }[] = [];
    mockApi({
      "POST /hdmi/destinations": (body) => {
        sent.push({ body });
        return { id: 2, device_id: MATRIX_DEVICE_ID, name: "Foyer", default_input_id: null, sort_order: 0, output_ids: [], updated_at: "now" };
      },
    });
    renderSection([]);

    fireEvent.click(screen.getByRole("button", { name: "+ Add" }));
    fireEvent.change(await screen.findByLabelText("Name"), { target: { value: "Foyer" } });

    // Select BOH Return before Main Projector — selection order is display order.
    fireEvent.click(screen.getByLabelText("BOH Return"));
    fireEvent.click(screen.getByLabelText("Main Projector"));
    const bohRow = screen.getByLabelText("BOH Return").closest("li") as HTMLElement;
    const projectorRow = screen.getByLabelText("Main Projector").closest("li") as HTMLElement;
    expect(within(bohRow).getByText("1st — display")).toBeInTheDocument();
    expect(within(projectorRow).getByText("2nd")).toBeInTheDocument();

    // Reorder: move BOH Return (currently first) later, so Main Projector leads.
    fireEvent.click(screen.getByRole("button", { name: "Move BOH Return later" }));

    fireEvent.click(screen.getByRole("button", { name: "Add destination" }));

    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0]?.body).toMatchObject({ output_ids: [1, 2] }); // Main Projector (1) now leads
  });

  it("sets the default input from the picker", async () => {
    const sent: { body?: unknown }[] = [];
    mockApi({
      "POST /hdmi/destinations": (body) => {
        sent.push({ body });
        return { id: 2, device_id: MATRIX_DEVICE_ID, name: "Foyer", default_input_id: 2, sort_order: 0, output_ids: [1], updated_at: "now" };
      },
    });
    renderSection([]);

    fireEvent.click(screen.getByRole("button", { name: "+ Add" }));
    fireEvent.change(await screen.findByLabelText("Name"), { target: { value: "Foyer" } });
    fireEvent.click(screen.getByLabelText("Main Projector"));
    fireEvent.change(screen.getByLabelText("Default input"), { target: { value: "2" } });
    fireEvent.click(screen.getByRole("button", { name: "Add destination" }));

    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0]?.body).toMatchObject({ default_input_id: 2 });
  });

  it("shows the scene reference on a delete blocked because it is in_use", async () => {
    mockApi({
      "DELETE /hdmi/destinations/1": () => {
        throw new ApiError(409, "in_use", "Still in use", { references: [{ entity: "scenes", id: 4, name: "Interval" }] });
      },
    });
    renderSection();

    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    const confirm = await screen.findByRole("alertdialog", { name: /Delete "The room"/ });
    fireEvent.click(within(confirm).getByRole("button", { name: "Delete" }));

    const guard = await screen.findByRole("alertdialog", { name: /Cannot delete/ });
    expect(within(guard).getByText("Interval")).toBeInTheDocument();
  });

  it("offers reload or overwrite on a version conflict", async () => {
    const current: HdmiDestination = { ...HDMI_DESTINATIONS[0]!, name: "The auditorium", updated_at: "2026-09-10T20:00:00+12:00" };
    mockApi({
      "PUT /hdmi/destinations/1": () => {
        throw new ApiError(409, "conflict", "This was changed by someone else", { current });
      },
    });
    renderSection();

    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.change(await screen.findByLabelText("Name"), { target: { value: "Mine" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    const dialog = await screen.findByRole("alertdialog");
    expect(within(dialog).getByText(/was changed by someone else/)).toBeInTheDocument();
    expect(within(dialog).getByText("The auditorium")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Overwrite with mine" })).toBeInTheDocument();
  });
});
