/*
 * Changing a driver and re-mapping its references (spec §5.5, §21.24).
 *
 * The flow is two steps in one sheet: the new driver's settings beside the
 * rows that will need a new reference, then the re-mapping screen. These
 * tests hold the rules the screen owns: what it pre-selects and offers, that
 * a mixer channel may be left unmapped and a matrix row may not, that a
 * reverted driver change never reaches the re-mapping step, and that what is
 * posted is exactly what the admin chose.
 */
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/api/client";
import { describeMissing, findMissingHelp } from "@/help/coverage";
import { renderWithProviders } from "@/test/render";

import { DeviceCard } from "./DeviceCard";
import { ChangeDriverSheet } from "./RemapSheet";
import { buildChoices } from "./remap";
import type { Device, Driver, RemapResponse, RemapRow } from "./types";

const client = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

const LOOPBACK = [{ type: "loopback", config_schema: [], defaults: {} }];

function mixerDriver(key: string, name: string): Driver {
  return { key, category: "mixer", name, transports: LOOPBACK, config_schema: [], capabilities: {} };
}

const CQ = mixerDriver("cq20b", "Allen & Heath CQ-20B");
const STUB = mixerDriver("stub", "Stub mixer (no hardware)");

const DESK: Device = {
  id: 4,
  category: "mixer",
  driver_key: "cq20b",
  name: "Desk",
  enabled: true,
  config: { transport: { type: "loopback" }, driver: {} },
  created_at: "2026-09-01T09:00:00+12:00",
  updated_at: "2026-09-10T19:42:11+12:00",
  state_key: "mixer",
  status: { status: "connected", detail: null },
};

function row(overrides: Partial<RemapRow> & Pick<RemapRow, "id" | "name" | "old_refs">): RemapRow {
  return {
    holder: "mixer_channel",
    kind: "input",
    unmapped: false,
    new_refs: overrides.old_refs.map(() => null),
    ...overrides,
  };
}

const BEFORE: RemapResponse = {
  device_id: 4,
  driver_key: "cq20b",
  as_connected: true,
  mappings: [
    row({ id: 1, name: "Main LR", kind: "main", old_refs: ["main"], new_refs: ["main"] }),
    row({ id: 2, name: "Wireless 1", old_refs: ["ip1"], new_refs: ["ip1"] }),
    row({ id: 3, name: "Stage pair", old_refs: ["ip2", "ip3"], new_refs: ["ip2", "ip3"] }),
  ],
  available: { refs: [] },
  missing_channels: 0,
};

const AFTER: RemapResponse = {
  device_id: 4,
  driver_key: "stub",
  as_connected: true,
  mappings: [
    row({ id: 1, name: "Main LR", kind: "main", unmapped: true, old_refs: ["main"], new_refs: ["main"] }),
    row({ id: 2, name: "Wireless 1", unmapped: true, old_refs: ["ip1"] }),
    row({ id: 3, name: "Stage pair", unmapped: true, old_refs: ["ip2", "ip3"] }),
  ],
  available: {
    refs: [
      { ref: "main", label: "Main", kind: "main", stereo: true },
      { ref: "in1", label: "Input 1", kind: "input", stereo: false },
      { ref: "in2", label: "Input 2", kind: "input", stereo: false },
      { ref: "out1", label: "Output 1", kind: "output", stereo: false },
    ],
  },
  missing_channels: 0,
};

interface Call {
  path: string;
  method: string;
  body: unknown;
}

function serve({
  put = () => Promise.resolve({ ...DESK, driver_key: "stub" }),
  post = () => Promise.resolve(AFTER),
}: {
  put?: () => Promise<unknown>;
  post?: () => Promise<unknown>;
} = {}): Call[] {
  const calls: Call[] = [];
  let changed = false;
  client.api.mockImplementation((path: string, options?: { method?: string; body?: unknown }) => {
    const method = options?.method ?? (options?.body !== undefined ? "POST" : "GET");
    calls.push({ path, method, body: options?.body });
    if (path === "/devices/4" && method === "PUT") {
      return put().then((result) => {
        changed = true;
        return result;
      });
    }
    if (path === "/devices/4/remap" && method === "GET") {
      // After the change the answer arrives late, as it does over a network:
      // the cached answer from before the change must not seed the pickers.
      return changed ? new Promise((resolve) => setTimeout(() => resolve(AFTER), 50)) : Promise.resolve(BEFORE);
    }
    if (path === "/devices/4/remap" && method === "POST") return post();
    return Promise.resolve({});
  });
  return calls;
}

function renderSheet(target: Driver | null = STUB, onFinished = vi.fn()) {
  const onOpenChange = vi.fn();
  renderWithProviders(
    <ChangeDriverSheet open onOpenChange={onOpenChange} device={DESK} target={target} onFinished={onFinished} />,
    { route: "/admin/devices", status: "authenticated", tier: "admin" },
  );
  return { onOpenChange, onFinished };
}

async function toRemapStep() {
  const dialog = await screen.findByRole("dialog", { name: "Change Desk to Stub mixer (no hardware)" });
  await within(dialog).findByText("Stage pair");
  fireEvent.click(within(dialog).getByRole("button", { name: "Change driver" }));
  await screen.findByRole("dialog", { name: "Re-map Desk's references" });
  await screen.findByLabelText("New reference for Wireless 1");
}

describe("ChangeDriverSheet", () => {
  beforeEach(() => {
    client.api.mockReset();
  });

  it("lists what will need a new reference before the driver is changed", async () => {
    const calls = serve();
    renderSheet();

    const dialog = await screen.findByRole("dialog", { name: "Change Desk to Stub mixer (no hardware)" });
    const list = await within(dialog).findByRole("list", { name: "Rows holding a reference to this device" });
    expect(within(list).getByText("Wireless 1")).toBeInTheDocument();
    expect(within(list).getByText("ip2 + ip3")).toBeInTheDocument();
    expect(calls.some((call) => call.method === "PUT")).toBe(false);
  });

  it("changes the driver, then pre-selects only what the new driver declares, and posts the choices", async () => {
    const calls = serve();
    const { onFinished, onOpenChange } = renderSheet();

    await toRemapStep();

    const put = calls.find((call) => call.method === "PUT");
    expect(put?.body).toEqual({ driver_key: "stub", config: { transport: { type: "loopback" }, driver: {} } });

    const main = await screen.findByLabelText("New reference for Main LR");
    expect(main).toHaveValue("main");
    // Main is offered only the new driver's Main (§7.3); an input never is.
    expect(within(main).getAllByRole("option").map((option) => option.getAttribute("value"))).toEqual(["", "main"]);
    const wireless = screen.getByLabelText("New reference for Wireless 1");
    expect(wireless).toHaveValue(""); // no positional guess
    expect(within(wireless).queryByRole("option", { name: "Main (main)" })).toBeNull();

    fireEvent.change(wireless, { target: { value: "in1" } });
    fireEvent.change(screen.getByLabelText("New reference for Stage pair (was ip2)"), { target: { value: "in2" } });
    // One side of the pair is left blank, so the whole channel stays unmapped.
    expect(screen.getByText(/1 channel will be left unmapped/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Apply re-mapping" }));

    await waitFor(() => expect(onFinished).toHaveBeenCalled());
    const post = calls.find((call) => call.method === "POST" && call.path === "/devices/4/remap");
    expect(post?.body).toEqual({
      mappings: [
        { holder: "mixer_channel", id: 1, new_refs: ["main"] },
        { holder: "mixer_channel", id: 2, new_refs: ["in1"] },
        { holder: "mixer_channel", id: 3, new_refs: null },
      ],
    });
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("offers the desk channels no channel covers once applied, and adds them only when asked", async () => {
    const calls = serve({ post: () => Promise.resolve({ ...AFTER, missing_channels: 3 }) });
    const { onFinished, onOpenChange } = renderSheet();

    await toRemapStep();
    fireEvent.click(screen.getByRole("button", { name: "Apply re-mapping" }));

    // Not added silently: the sheet stays open and asks.
    const add = await screen.findByRole("button", { name: "Add missing channels" });
    expect(screen.getByText(/The new driver has 3 desk channels that no channel of Desk points at/)).toBeInTheDocument();
    expect(onFinished).not.toHaveBeenCalled();
    expect(calls.some((call) => call.path === "/mixer/devices/4/missing-channels")).toBe(false);

    const serveAdded = client.api.getMockImplementation();
    client.api.mockImplementation((path: string, options?: { method?: string; body?: unknown }) => {
      if (path === "/mixer/devices/4/missing-channels" && options?.method === "POST") {
        calls.push({ path, method: "POST", body: undefined });
        return Promise.resolve({ device_id: 4, created: [{ id: 10 }, { id: 11 }, { id: 12 }] });
      }
      return serveAdded ? serveAdded(path, options) : Promise.resolve({});
    });
    fireEvent.click(add);

    await waitFor(() =>
      expect(onFinished).toHaveBeenCalledWith("Desk's references are re-mapped; 3 channels are left unmapped. Added 3 channels."),
    );
    expect(calls.filter((call) => call.path === "/mixer/devices/4/missing-channels")).toHaveLength(1);
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("closes without adding anything when the offer is declined", async () => {
    const calls = serve({ post: () => Promise.resolve({ ...AFTER, missing_channels: 2 }) });
    const { onFinished } = renderSheet();

    await toRemapStep();
    fireEvent.click(screen.getByRole("button", { name: "Apply re-mapping" }));
    fireEvent.click(await screen.findByRole("button", { name: "Not now" }));

    expect(onFinished).toHaveBeenCalledWith("Desk's references are re-mapped; 3 channels are left unmapped.");
    expect(calls.some((call) => call.path === "/mixer/devices/4/missing-channels")).toBe(false);
  });

  it("stays on the settings step, and says nothing changed, when the new driver cannot connect", async () => {
    const calls = serve({
      put: () =>
        Promise.reject(
          new ApiError(503, "device_unavailable", "The new settings could not reach the device", {
            reason: "no reply",
            reverted: true,
          }),
        ),
    });
    renderSheet();

    const dialog = await screen.findByRole("dialog", { name: "Change Desk to Stub mixer (no hardware)" });
    await within(dialog).findByText("Stage pair");
    fireEvent.click(within(dialog).getByRole("button", { name: "Change driver" }));

    expect(await screen.findByText(/The new driver could not reach the device/)).toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: "Re-map Desk's references" })).toBeNull();
    expect(calls.some((call) => call.method === "POST")).toBe(false);
  });

  it("refuses to leave a matrix row unmapped without asking the API", async () => {
    const matrix: RemapResponse = {
      device_id: 4,
      driver_key: "stub",
      as_connected: true,
      mappings: [row({ holder: "matrix_input", id: 9, name: "Stage feed", old_refs: ["1"] })],
      available: { inputs: [{ ref: "A", label: "In A", kind: "input", stereo: false }], outputs: [] },
      missing_channels: 0,
    };
    client.api.mockImplementation((path: string, options?: { body?: unknown }) => {
      if (path === "/devices/4/remap" && options?.body === undefined) return Promise.resolve(matrix);
      return Promise.resolve({});
    });
    renderSheet(null);

    const picker = await screen.findByLabelText("New reference for Stage feed");
    expect(within(picker).getByRole("option", { name: "Choose a reference" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Apply re-mapping" }));

    expect(await screen.findByText(/a matrix input cannot be left unmapped/)).toBeInTheDocument();
    expect(client.api.mock.calls.some(([, options]) => (options as { body?: unknown } | undefined)?.body)).toBe(false);
  });

  it("shows the API's refusal per row and says nothing was changed", async () => {
    serve({
      post: () =>
        Promise.reject(
          new ApiError(422, "validation_failed", "Some of the re-mapping cannot be applied", {
            "mixer_channel:2": ["the new driver has no reference 'in1'"],
          }),
        ),
    });
    renderSheet();
    await toRemapStep();

    fireEvent.click(screen.getByRole("button", { name: "Apply re-mapping" }));

    expect(await screen.findByText("the new driver has no reference 'in1'")).toBeInTheDocument();
    expect(screen.getByText(/Nothing was changed/)).toBeInTheDocument();
    expect(screen.getByLabelText("New reference for Wireless 1")).toHaveAttribute("aria-invalid", "true");
  });

  it("gives every primary action on both steps its help (spec §19.1)", async () => {
    serve();
    renderSheet();
    const dialog = await screen.findByRole("dialog", { name: "Change Desk to Stub mixer (no hardware)" });
    await within(dialog).findByText("Stage pair");
    expect(findMissingHelp(document.body), describeMissing(findMissingHelp(document.body))).toEqual([]);

    await toRemapStep();
    await screen.findByLabelText("New reference for Wireless 1");
    expect(findMissingHelp(document.body), describeMissing(findMissingHelp(document.body))).toEqual([]);
    expect(screen.getByRole("button", { name: "Help: New reference" })).toBeInTheDocument();
  });
});

describe("DeviceCard", () => {
  beforeEach(() => {
    client.api.mockReset();
  });

  it("opens the re-mapping step directly for a device whose rows hold references", async () => {
    serve();
    renderWithProviders(<DeviceCard device={DESK} driver={CQ} alternatives={[CQ, STUB]} />, {
      route: "/admin/devices",
      status: "authenticated",
      tier: "admin",
    });

    fireEvent.click(screen.getByRole("button", { name: "Re-map references" }));

    expect(await screen.findByRole("dialog", { name: "Re-map Desk's references" })).toBeInTheDocument();
    expect(await screen.findByLabelText("New reference for Wireless 1")).toBeInTheDocument();
  });
});

describe("buildChoices", () => {
  it("leaves a channel unmapped when any of its references has no new one", () => {
    const rows = AFTER.mappings;
    expect(buildChoices(rows, { "mixer_channel:1": ["main"], "mixer_channel:3": ["in1", ""] })).toEqual([
      { holder: "mixer_channel", id: 1, new_refs: ["main"] },
      { holder: "mixer_channel", id: 2, new_refs: null },
      { holder: "mixer_channel", id: 3, new_refs: null },
    ]);
  });
});
