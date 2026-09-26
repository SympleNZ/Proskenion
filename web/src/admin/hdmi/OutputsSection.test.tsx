/*
 * Admin → HDMI → Outputs (§21.22): the same ref-and-name pattern as Inputs,
 * plus the Destination column, which names whichever destination currently
 * lists an output.
 */
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "@/test/render";

import { HDMI_DESTINATIONS, HDMI_OUTPUTS, LKV422_OUTPUT_REFS, MATRIX_DEVICE_ID } from "./fixtures";
import { OutputsSection } from "./OutputsSection";

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

function rowFor(container: HTMLElement, ref: string): HTMLElement {
  const row = container.querySelector(`tr[data-ref="${ref}"]`);
  if (!row) throw new Error(`no row for ref ${ref}`);
  return row as HTMLElement;
}

describe("OutputsSection", () => {
  beforeEach(() => {
    client.api.mockReset();
    mockApi({});
  });

  it("names the destination that claims each output, and none for one that isn't claimed", () => {
    const { container } = renderWithProviders(
      <OutputsSection deviceId={MATRIX_DEVICE_ID} outputs={HDMI_OUTPUTS} refs={LKV422_OUTPUT_REFS} destinations={HDMI_DESTINATIONS} />,
      { route: "/admin/hdmi" },
    );

    expect(within(rowFor(container, "1")).getByText("The room")).toBeInTheDocument();
    expect(within(rowFor(container, "2")).getByText("The room")).toBeInTheDocument();
  });

  it("shows an unclaimed destination as 'None yet' for a named output outside any destination", () => {
    const { container } = renderWithProviders(
      <OutputsSection deviceId={MATRIX_DEVICE_ID} outputs={HDMI_OUTPUTS} refs={LKV422_OUTPUT_REFS} destinations={[]} />,
      { route: "/admin/hdmi" },
    );

    expect(within(rowFor(container, "1")).getByText("None yet")).toBeInTheDocument();
  });

  it("creates a named output from an unclaimed ref, restricted to output refs", async () => {
    const sent: { body?: unknown }[] = [];
    mockApi({
      "POST /hdmi/outputs": (body) => {
        sent.push({ body });
        return { id: 3, device_id: MATRIX_DEVICE_ID, driver_ref: "3", name: "Foyer screen", description: null, sort_order: 0, updated_at: "now" };
      },
    });
    const refsWithSpare = [...LKV422_OUTPUT_REFS, { ref: "3", label: "Output 3", kind: "output" as const, stereo: false }];
    const { container } = renderWithProviders(
      <OutputsSection deviceId={MATRIX_DEVICE_ID} outputs={HDMI_OUTPUTS} refs={refsWithSpare} destinations={HDMI_DESTINATIONS} />,
      { route: "/admin/hdmi" },
    );

    fireEvent.click(within(rowFor(container, "3")).getByRole("button", { name: "Name it" }));
    const picker = await screen.findByLabelText("Physical output");
    expect(within(picker).getAllByRole("option").map((option) => option.textContent)).toEqual(["Output 3"]);
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Foyer screen" } });
    fireEvent.click(screen.getByRole("button", { name: "Add output" }));

    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0]?.body).toEqual({ device_id: MATRIX_DEVICE_ID, driver_ref: "3", name: "Foyer screen", description: null, sort_order: 0 });
  });
});
