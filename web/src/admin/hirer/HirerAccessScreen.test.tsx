/*
 * Admin → Hirer Access (§21.20, docs/plans/phase-5-contracts.md "Hirer
 * configuration"). `GET`/`PUT /hirer/config`, `GET /pages` and
 * `GET /hirer/conflicts` are mocked at the `api()` boundary exactly as the
 * contract shapes them, matching `proskenion/api/hirer.py`.
 */
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { PageDetail, PageListItem } from "@/admin/pages/types";
import { renderWithProviders } from "@/test/render";

import { HirerAccessScreen } from "./HirerAccessScreen";
import type { ConflictsResponse, HirerConfig } from "./types";

const client = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

type Handler = (body: unknown, headers: Record<string, string> | undefined) => unknown;

function mockApi(handlers: Record<string, Handler>) {
  client.api.mockImplementation((path: string, options?: { method?: string; body?: unknown; headers?: Record<string, string> }) => {
    const method = options?.method ?? (options?.body === undefined ? "GET" : "POST");
    const handler = handlers[`${method} ${path}`];
    if (!handler) throw new Error(`unhandled request: ${method} ${path}`);
    return Promise.resolve(handler(options?.body, options?.headers));
  });
}

const HIRE: PageListItem = { id: 1, name: "Hire", sort_order: 0, is_default: false, hirer: true, updated_at: "2026-09-19T09:00:00+12:00" };
const LIGHTING: PageListItem = { id: 2, name: "Lighting", sort_order: 1, is_default: false, hirer: false, updated_at: "2026-09-19T09:00:00+12:00" };
const DEFAULT: PageListItem = { id: 3, name: "Default", sort_order: 2, is_default: true, hirer: false, updated_at: "2026-09-19T09:00:00+12:00" };

const HIRE_DETAIL: PageDetail = {
  ...HIRE,
  items: [
    { id: 10, sort_order: 0, kind: "channel", source: "mixer", channel_id: 5, channel: { name: "Wireless 1", short_name: "WL1", channel_kind: "input" } },
    { id: 11, sort_order: 1, kind: "channel", source: "mixer", channel_id: 9, channel: { name: "Main", short_name: "Main", channel_kind: "main" } },
    { id: 12, sort_order: 2, kind: "channel", source: "mixer", channel_id: 7, channel: { name: "Foyer Speakers", short_name: "Foyer", channel_kind: "output" } },
  ],
};

const LIGHTING_DETAIL: PageDetail = { ...LIGHTING, items: [] };

function baseConfig(overrides: Partial<HirerConfig> = {}): HirerConfig {
  return {
    enabled: true,
    pin_is_placeholder: false,
    pages: [1],
    ceilings: [
      { channel_id: 5, name: "Wireless 1", channel_kind: "input", hirer_max_db: -3 },
      { channel_id: 9, name: "Main", channel_kind: "main", hirer_max_db: null },
    ],
    lighting_enabled: true,
    individual_fixtures: true,
    colour_enabled: true,
    updated_at: "2026-09-19T09:00:00+12:00",
    ...overrides,
  };
}

const BASE_HANDLERS: Record<string, Handler> = {
  "GET /pages": () => ({ pages: [HIRE, LIGHTING, DEFAULT] }),
  "GET /pages/1": () => HIRE_DETAIL,
  "GET /pages/2": () => LIGHTING_DETAIL,
  "GET /mixer/state": () => ({ device_id: null, capabilities: { scene_recall: false } }),
  "GET /hirer/conflicts": () => ({ conflicts: [] }) satisfies ConflictsResponse,
};

describe("HirerAccessScreen", () => {
  beforeEach(() => {
    client.api.mockReset();
  });

  it("lists assigned and unassigned pages, never offers the default page, and shows reachable ceilings", async () => {
    mockApi({ ...BASE_HANDLERS, "GET /hirer/config": () => baseConfig() });
    renderWithProviders(<HirerAccessScreen />, { route: "/admin/hirer-access" });

    expect(await screen.findByText("Hire")).toBeInTheDocument();
    expect(screen.getByText("Lighting")).toBeInTheDocument();
    expect(screen.queryByText("Default")).not.toBeInTheDocument();

    // The assigned page's checkbox starts ticked; the other does not.
    expect(screen.getByLabelText('Assign "Hire"')).toBeChecked();
    expect(screen.getByLabelText('Assign "Lighting"')).not.toBeChecked();

    // Only the input and Main are offered a ceiling — never the output.
    expect(await screen.findByText("Wireless 1")).toBeInTheDocument();
    expect(screen.getAllByText("Main").length).toBeGreaterThan(0);
    expect(screen.queryByText("Foyer Speakers", { selector: ".field-label" })).not.toBeInTheDocument();

    // The output on the assigned page is flagged rather than silently dropped (§21.20).
    expect(await screen.findByText(/Foyer Speakers.*is on.*Hire.*cannot be reached by a hirer/s)).toBeInTheDocument();
  });

  it("sends a PUT body matching the contract exactly, with the version header", async () => {
    const puts: { body: unknown; headers: Record<string, string> | undefined }[] = [];
    mockApi({
      ...BASE_HANDLERS,
      "GET /hirer/config": () => baseConfig(),
      "PUT /hirer/config": (body, headers) => {
        puts.push({ body, headers });
        return baseConfig({ pages: [1, 2], updated_at: "2026-09-19T09:05:00+12:00" });
      },
    });
    renderWithProviders(<HirerAccessScreen />, { route: "/admin/hirer-access" });

    fireEvent.click(await screen.findByLabelText('Assign "Lighting"'));
    fireEvent.click(await screen.findByRole("button", { name: "Save changes" }));

    await waitFor(() => expect(puts).toHaveLength(1));
    expect(puts[0]?.headers).toEqual({ "If-Unmodified-Since-Version": "2026-09-19T09:00:00+12:00" });
    expect(puts[0]?.body).toEqual({
      pages: [1, 2],
      ceilings: [
        { channel_id: 5, hirer_max_db: -3 },
        { channel_id: 9, hirer_max_db: null },
      ],
      lighting_enabled: true,
      individual_fixtures: true,
      colour_enabled: true,
    });
  });

  it("opens the admin conflict dialog on a 409", async () => {
    const { ApiError } = await import("@/api/client");
    const current = baseConfig({ colour_enabled: false, updated_at: "2026-09-19T09:10:00+12:00" });
    mockApi({
      ...BASE_HANDLERS,
      "GET /hirer/config": () => baseConfig(),
      "PUT /hirer/config": () => {
        throw new ApiError(409, "conflict", "This was changed by someone else", { current });
      },
    });
    renderWithProviders(<HirerAccessScreen />, { route: "/admin/hirer-access" });

    fireEvent.click(await screen.findByLabelText('Assign "Lighting"'));
    fireEvent.click(await screen.findByRole("button", { name: "Save changes" }));

    const dialog = await screen.findByRole("alertdialog", { name: /Hirer access was changed by someone else/ });
    expect(within(dialog).getByRole("button", { name: "Overwrite with mine" })).toBeInTheDocument();
  });

  it("shows every conflict kind, including a desk scene never recalled (observed: false)", async () => {
    const conflicts: ConflictsResponse = {
      conflicts: [
        {
          channel_id: 5,
          channel_name: "Wireless 1",
          ceiling_db: -5,
          level_db: -1,
          source: { kind: "desk_scene", desk_scene_id: 2, name: "Band" },
        },
        {
          channel_id: 5,
          channel_name: "Wireless 1",
          ceiling_db: -5,
          level_db: null,
          observed: false,
          source: { kind: "desk_scene", desk_scene_id: 3, name: "Band 2" },
        },
        {
          channel_id: 9,
          channel_name: "Main",
          ceiling_db: -3,
          level_db: 0,
          source: { kind: "scene_action", scene_id: 4, action_id: 11, name: "Interval" },
        },
      ],
    };
    mockApi({ ...BASE_HANDLERS, "GET /hirer/config": () => baseConfig(), "GET /hirer/conflicts": () => conflicts });
    renderWithProviders(<HirerAccessScreen />, { route: "/admin/hirer-access" });

    expect(await screen.findByText(/“Band” recalls Wireless 1/)).toBeInTheDocument();
    expect(await screen.findByText(/“Band 2” has never been recalled/)).toBeInTheDocument();
    expect(await screen.findByText(/not yet checked/)).toBeInTheDocument();
    expect(await screen.findByText(/“Interval” sets Main to 0\.0 dB/)).toBeInTheDocument();
  });
});
