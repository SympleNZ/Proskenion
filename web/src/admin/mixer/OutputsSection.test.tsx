/*
 * Admin → Mixer → Mix outputs (§21.21, §7.3 *Linked stereo outputs*): the
 * linked-pair picker — the pair's own reference from `available_refs()`,
 * because link state cannot be read back over MIDI — carries §7.3's warning
 * text next to it, verbatim, and the picker's options carry human labels.
 */
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "@/test/render";

import { CQ20B_FADER_LAW, CQ20B_REFS, MIXER_DEVICE_ID, OUTPUT_CHANNEL } from "./fixtures";
import { OutputsSection } from "./OutputsSection";

const client = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

function mockApi(handlers: Record<string, (body?: unknown) => unknown>) {
  client.api.mockImplementation((path: string, options?: { method?: string; body?: unknown }) => {
    const method = options?.method ?? (options?.body === undefined ? "GET" : "POST");
    const handler = handlers[`${method} ${path}`];
    return Promise.resolve(handler ? handler(options?.body) : undefined);
  });
}

describe("OutputsSection", () => {
  beforeEach(() => {
    client.api.mockReset();
    mockApi({});
  });

  it("flags an output a driver change left unmapped, first, with a warning (§5.5)", () => {
    const unmapped = { ...OUTPUT_CHANNEL, id: 12, name: "Stage monitors", sort_order: 9, unmapped: true };
    renderWithProviders(
      <OutputsSection deviceId={MIXER_DEVICE_ID} outputs={[OUTPUT_CHANNEL, unmapped]} refs={CQ20B_REFS} faderLaw={CQ20B_FADER_LAW} />,
      { route: "/admin/mixer" },
    );

    expect(screen.getByText(/1 output lost its reference in a driver change/)).toBeInTheDocument();
    const rows = screen.getAllByRole("row").slice(1); // drop the header row
    expect(rows[0]).toHaveTextContent("Stage monitors");
    expect(rows[0]).toHaveTextContent("Unmapped");
  });

  it("labels reference picker options with the driver's human label and its opaque ref", async () => {
    renderWithProviders(<OutputsSection deviceId={MIXER_DEVICE_ID} outputs={[]} refs={CQ20B_REFS} faderLaw={CQ20B_FADER_LAW} />, {
      route: "/admin/mixer",
    });

    fireEvent.click(screen.getByRole("button", { name: "+ Add output" }));
    const picker = await screen.findByLabelText("Maps to");
    const options = Array.from(picker.querySelectorAll("option")).map((option) => option.textContent);

    expect(options).toContain("Out 1 (out1)");
    expect(options).toContain("Out 1/2 (linked) — out12");
  });

  it("chooses a linked pair by its own reference, with §7.3's warning shown next to the picker", async () => {
    const sent: { body?: unknown }[] = [];
    mockApi({
      "POST /mixer/channels": (body) => {
        sent.push({ body });
        return { ...OUTPUT_CHANNEL, id: 9, driver_refs: ["out56"] };
      },
    });
    renderWithProviders(<OutputsSection deviceId={MIXER_DEVICE_ID} outputs={[]} refs={CQ20B_REFS} faderLaw={CQ20B_FADER_LAW} />, {
      route: "/admin/mixer",
    });

    fireEvent.click(screen.getByRole("button", { name: "+ Add output" }));
    expect(await screen.findByText(/The mixer does not report link state over MIDI/)).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Maps to"), { target: { value: "out56" } });
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Recording Pair" } });
    fireEvent.click(screen.getByRole("button", { name: "Add output" }));

    await waitFor(() => expect(sent).toHaveLength(1));
    const body = sent[0]?.body as { driver_refs: string[] };
    expect(body.driver_refs).toEqual(["out56"]);
  });

  it("restricts the picker to output references, never input ones", async () => {
    renderWithProviders(<OutputsSection deviceId={MIXER_DEVICE_ID} outputs={[]} refs={CQ20B_REFS} faderLaw={CQ20B_FADER_LAW} />, {
      route: "/admin/mixer",
    });

    fireEvent.click(screen.getByRole("button", { name: "+ Add output" }));
    const picker = await screen.findByLabelText("Maps to");
    const options = Array.from(picker.querySelectorAll("option")).map((option) => option.textContent);

    expect(options.some((label) => label?.startsWith("Input"))).toBe(false);
  });
});
