/*
 * Admin → Mixer → Channels (§21.21, §5.5): ganged references keep the order
 * they were added in (the first stays authoritative), `hirer_max_db` is
 * edited and shown as dB — never a wire value — unmapped channels are
 * flagged and sorted first, a delete blocked by a scene action shows the
 * references, and a concurrent edit offers the conflict dialog.
 */
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/api/client";
import { renderWithProviders } from "@/test/render";

import { ChannelsSection } from "./ChannelsSection";
import { CQ20B_FADER_LAW, CQ20B_REFS, GANGED_INPUT_CHANNEL, INPUT_CHANNEL, MIXER_DEVICE_ID, UNMAPPED_CHANNEL } from "./fixtures";

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

describe("ChannelsSection", () => {
  beforeEach(() => {
    client.api.mockReset();
    mockApi({});
  });

  it("gangs a second reference onto a channel and saves them in the order added, first authoritative", async () => {
    const sent: { body?: unknown }[] = [];
    mockApi({
      "POST /mixer/channels": (body) => {
        sent.push({ body });
        return { ...INPUT_CHANNEL, id: 20, driver_refs: (body as { driver_refs: string[] }).driver_refs };
      },
    });
    renderWithProviders(<ChannelsSection deviceId={MIXER_DEVICE_ID} channels={[]} refs={CQ20B_REFS} faderLaw={CQ20B_FADER_LAW} />, {
      route: "/admin/mixer",
    });

    fireEvent.click(screen.getByRole("button", { name: "+ Add channel" }));
    fireEvent.change(await screen.findByLabelText("Maps to"), { target: { value: "ip1" } });
    expect(screen.getByText("authoritative")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "+ Gang another" }));
    expect(screen.getByText("#2")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Lapel Pair" } });
    fireEvent.click(screen.getByRole("button", { name: "Add channel" }));

    await waitFor(() => expect(sent).toHaveLength(1));
    const body = sent[0]?.body as { driver_refs: string[] };
    expect(body.driver_refs).toEqual(["ip1", "ip2"]);
  });

  it("edits hirer_max_db as dB, never the mixer's wire value", async () => {
    const sent: { body?: unknown }[] = [];
    mockApi({
      "POST /mixer/channels": (body) => {
        sent.push({ body });
        return { ...INPUT_CHANNEL, id: 21 };
      },
    });
    renderWithProviders(<ChannelsSection deviceId={MIXER_DEVICE_ID} channels={[]} refs={CQ20B_REFS} faderLaw={CQ20B_FADER_LAW} />, {
      route: "/admin/mixer",
    });

    fireEvent.click(screen.getByRole("button", { name: "+ Add channel" }));
    expect(screen.getByLabelText("No ceiling")).toBeChecked();
    fireEvent.click(screen.getByLabelText("No ceiling"));

    expect(screen.getByText("-5.0")).toBeInTheDocument();
    fireEvent.change(screen.getByRole("slider"), { target: { value: "766" } });
    expect(screen.getByText("0.0")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Maps to"), { target: { value: "ip1" } });
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Wireless Mic 1" } });
    fireEvent.click(screen.getByRole("button", { name: "Add channel" }));

    await waitFor(() => expect(sent).toHaveLength(1));
    const body = sent[0]?.body as { hirer_max_db: number | null };
    expect(body.hirer_max_db).toBe(0);
  });

  it("flags unmapped channels and lists them first, regardless of sort_order", () => {
    renderWithProviders(<ChannelsSection deviceId={MIXER_DEVICE_ID} channels={[INPUT_CHANNEL, UNMAPPED_CHANNEL]} refs={CQ20B_REFS} faderLaw={CQ20B_FADER_LAW} />, {
      route: "/admin/mixer",
    });

    expect(screen.getByText("Unmapped")).toBeInTheDocument();
    const rows = screen.getAllByRole("row").slice(1); // drop the header row
    expect(within(rows[0] as HTMLElement).getByText(UNMAPPED_CHANNEL.name)).toBeInTheDocument();
  });

  it("shows the reference list on an in_use delete refusal", async () => {
    mockApi({
      "DELETE /mixer/channels/4": () =>
        Promise.reject(
          new ApiError(409, "in_use", "Still in use", {
            references: [{ entity: "scene_actions", id: 11, name: "House to Half" }],
          }),
        ),
    });
    renderWithProviders(<ChannelsSection deviceId={MIXER_DEVICE_ID} channels={[GANGED_INPUT_CHANNEL]} refs={CQ20B_REFS} faderLaw={CQ20B_FADER_LAW} />, {
      route: "/admin/mixer",
    });

    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    const dialog = await screen.findByRole("alertdialog", { name: `Delete "${GANGED_INPUT_CHANNEL.name}"?` });
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete" }));

    expect(await screen.findByText("House to Half")).toBeInTheDocument();
  });

  it("offers the conflict dialog on a concurrent edit", async () => {
    mockApi({
      "PUT /mixer/channels/3": () =>
        Promise.reject(
          new ApiError(409, "conflict", "Changed elsewhere", {
            current: { ...INPUT_CHANNEL, name: "Renamed Elsewhere", updated_at: "2026-09-02T09:00:00+12:00" },
          }),
        ),
    });
    renderWithProviders(<ChannelsSection deviceId={MIXER_DEVICE_ID} channels={[INPUT_CHANNEL]} refs={CQ20B_REFS} faderLaw={CQ20B_FADER_LAW} />, {
      route: "/admin/mixer",
    });

    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.click(await screen.findByRole("button", { name: "Save" }));

    expect(await screen.findByText(/was changed by someone else/)).toBeInTheDocument();
  });
});
