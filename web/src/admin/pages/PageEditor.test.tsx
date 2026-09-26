/*
 * The Pages admin editor (docs/plans/phase-5-contracts.md "Pages", §21.9):
 * the `PUT` body matches the contract's stored-fields-only shape exactly, a
 * concurrent edit offers the §21.27 conflict treatment, the generated
 * default page is read-only, validate findings render against the item they
 * name with the duplicate-member offer, a button's row is never clamped
 * (Q5), panel width stays 1-4, and a button's colour is one of the closed
 * palette tokens.
 */
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/api/client";
import { renderWithProviders } from "@/test/render";

import { PageEditor } from "./PageEditor";
import type { PageDetail, PutPageBody } from "./types";

const client = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

function mockApi(handlers: Record<string, (body?: unknown) => unknown>) {
  client.api.mockImplementation((path: string, options?: { method?: string; body?: unknown }) => {
    const method = options?.method ?? (options?.body === undefined ? "GET" : "POST");
    const handler = handlers[`${method} ${path}`];
    if (!handler) throw new Error(`unhandled request: ${method} ${path}`);
    return Promise.resolve(handler(options?.body));
  });
}

const EMPTY_PAGE: PageDetail = {
  id: 5,
  name: "Performance",
  sort_order: 0,
  is_default: false,
  hirer: false,
  updated_at: "2026-09-19T09:00:00+12:00",
  items: [],
};

const DEFAULT_PAGE: PageDetail = {
  id: 6,
  name: "Default",
  sort_order: 1,
  is_default: true,
  hirer: false,
  updated_at: "2026-09-19T09:00:00+12:00",
  items: [
    {
      id: 20,
      sort_order: 0,
      kind: "channel",
      source: "mixer",
      channel_id: 1,
      channel: { name: "Wireless 1", short_name: "WL1", channel_kind: "input" },
    },
  ],
};

const RULE = { id: 9, name: "House Full Up", enabled: true };
const DERIVED_STATUS = { id: 4, name: "House at 100%" };

/** The pickers every panel and channel item field reads, empty unless a test needs them. */
function pickerHandlers(overrides: Record<string, (body?: unknown) => unknown> = {}) {
  return {
    "GET /mixer/channels": () => ({ channels: [] }),
    "GET /lighting/channels": () => ({ channels: [] }),
    "GET /lighting/groups": () => ({ groups: [] }),
    "GET /rules": () => ({ rules: [RULE] }),
    "GET /derived-status": () => ({ derived_statuses: [DERIVED_STATUS] }),
    ...overrides,
  };
}

describe("PageEditor", () => {
  beforeEach(() => {
    client.api.mockReset();
  });

  it("saves a new panel button with the contract's stored-fields-only shape — rows unbounded, colour a palette token", async () => {
    const sent: { body?: unknown; headers?: Record<string, string> }[] = [];
    mockApi(
      pickerHandlers({
        "GET /pages/5": () => EMPTY_PAGE,
        "PUT /pages/5": (body) => {
          sent.push({ body });
          return { ...EMPTY_PAGE, ...(body as object), updated_at: "2026-09-19T09:05:00+12:00" };
        },
      }),
    );

    renderWithProviders(<PageEditor pageId={5} onClose={vi.fn()} />, { route: "/admin/pages" });

    // Adding an item selects it immediately, so its fields are already open.
    fireEvent.click(await screen.findByRole("button", { name: "+ Panel" }));

    fireEvent.change(screen.getByLabelText("Title"), { target: { value: "Room" } });
    fireEvent.change(screen.getByLabelText("Width"), { target: { value: "3" } });

    // The grid's first empty cell, top-left, opens the button sheet at (0, 0).
    fireEvent.click(screen.getAllByRole("button", { name: "+" })[0] as HTMLElement);

    fireEvent.change(await screen.findByLabelText("Label"), { target: { value: "House Full Up" } });
    // Rows are unbounded (Q5) — nothing in the editor caps this at 2.
    fireEvent.change(screen.getByLabelText("Row"), { target: { value: "41" } });
    fireEvent.change(screen.getByLabelText("Fires rule"), { target: { value: String(RULE.id) } });
    fireEvent.change(screen.getByLabelText("Lamp from"), { target: { value: String(DERIVED_STATUS.id) } });
    fireEvent.click(screen.getByRole("radio", { name: "Ocean" }));
    fireEvent.click(screen.getByLabelText("Confirm before firing"));
    fireEvent.click(screen.getByRole("button", { name: "Add button" }));

    fireEvent.click(screen.getByRole("button", { name: "Save page" }));

    await waitFor(() => expect(sent).toHaveLength(1));
    const body = sent[0]?.body as PutPageBody;
    expect(body.name).toBe("Performance");
    expect(body.items).toEqual([
      {
        sort_order: 0,
        kind: "panel",
        panel_title: "Room",
        panel_width: 3,
        buttons: [
          { col: 0, row: 41, label: "House Full Up", rule_id: RULE.id, state_id: DERIVED_STATUS.id, colour: "ocean", confirm: true },
        ],
      },
    ]);
    // A new item and a new button carry no id (contract).
    expect(body.items[0]).not.toHaveProperty("id");
    expect((body.items[0] as { buttons: { id?: number }[] }).buttons[0]).not.toHaveProperty("id");
  });

  it("offers exactly the four widths 1-4 and never a fifth", async () => {
    mockApi(pickerHandlers({ "GET /pages/5": () => EMPTY_PAGE }));
    renderWithProviders(<PageEditor pageId={5} onClose={vi.fn()} />, { route: "/admin/pages" });

    fireEvent.click(await screen.findByRole("button", { name: "+ Panel" }));

    const width = screen.getByLabelText("Width") as HTMLSelectElement;
    const options = Array.from(width.options).map((option) => option.textContent);
    expect(options).toEqual(["1 column", "2 columns", "3 columns", "4 columns"]);
  });

  it("shows the §21.27 conflict dialog on a concurrent save, with reload and overwrite", async () => {
    const conflicting: PageDetail = { ...EMPTY_PAGE, name: "Renamed Elsewhere", updated_at: "2026-09-19T10:00:00+12:00" };
    let putCount = 0;
    mockApi(
      pickerHandlers({
        "GET /pages/5": () => EMPTY_PAGE,
        "PUT /pages/5": () => {
          putCount += 1;
          if (putCount === 1) {
            return Promise.reject(new ApiError(409, "conflict", "Changed elsewhere", { current: conflicting }));
          }
          return { ...EMPTY_PAGE, name: "Mine", updated_at: "2026-09-19T10:05:00+12:00" };
        },
      }),
    );

    renderWithProviders(<PageEditor pageId={5} onClose={vi.fn()} />, { route: "/admin/pages" });

    fireEvent.change(await screen.findByLabelText("Name"), { target: { value: "Mine" } });
    fireEvent.click(screen.getByRole("button", { name: "Save page" }));

    const dialog = await screen.findByRole("alertdialog", { name: /was changed by someone else/ });
    expect(within(dialog).getByText(/Performance/)).toBeInTheDocument();

    fireEvent.click(within(dialog).getByRole("button", { name: "Overwrite with mine" }));
    await waitFor(() => expect(putCount).toBe(2));
  });

  it("resets the form to the server's version on Reload theirs", async () => {
    const conflicting: PageDetail = { ...EMPTY_PAGE, name: "Renamed Elsewhere", updated_at: "2026-09-19T10:00:00+12:00" };
    mockApi(
      pickerHandlers({
        "GET /pages/5": () => EMPTY_PAGE,
        "PUT /pages/5": () => Promise.reject(new ApiError(409, "conflict", "Changed elsewhere", { current: conflicting })),
      }),
    );

    renderWithProviders(<PageEditor pageId={5} onClose={vi.fn()} />, { route: "/admin/pages" });

    fireEvent.change(await screen.findByLabelText("Name"), { target: { value: "Mine" } });
    fireEvent.click(screen.getByRole("button", { name: "Save page" }));

    const dialog = await screen.findByRole("alertdialog", { name: /was changed by someone else/ });
    fireEvent.click(within(dialog).getByRole("button", { name: "Reload theirs" }));

    expect(await screen.findByDisplayValue("Renamed Elsewhere")).toBeInTheDocument();
  });

  it("shows the generated default page read-only, with no Save, Validate or add-item controls", async () => {
    mockApi(pickerHandlers({ "GET /pages/6": () => DEFAULT_PAGE }));
    renderWithProviders(<PageEditor pageId={6} onClose={vi.fn()} />, { route: "/admin/pages" });

    expect(await screen.findByText(/generated automatically/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save page" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Validate" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "+ Panel" })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Name")).not.toBeInTheDocument();
    expect(screen.getByText(/Mixer · Wireless 1/)).toBeInTheDocument();
  });

  it("renders validate findings against the item they name, and offers to remove duplicates", async () => {
    const pageWithDuplicate: PageDetail = {
      ...EMPTY_PAGE,
      items: [
        {
          id: 12,
          sort_order: 0,
          kind: "group_master",
          group_id: 2,
          expanded: false,
          group: { name: "Row 1", colour: "var(--group-azure)" },
          members: [3],
          tray: false,
        },
        {
          id: 11,
          sort_order: 1,
          kind: "channel",
          source: "lighting",
          lighting_channel_id: 3,
          channel: { name: "Stage Wash 3", type: "dmx" },
        },
      ],
    };
    mockApi(
      pickerHandlers({
        "GET /pages/5": () => pageWithDuplicate,
        "GET /pages/5/validate": () => ({
          findings: [
            { code: "not_contiguous", item_id: 12, message: "The master and its members are not adjacent." },
            { code: "duplicate_member", item_id: 11, message: "Already controlled by the Row 1 master." },
          ],
        }),
      }),
    );

    renderWithProviders(<PageEditor pageId={5} onClose={vi.fn()} />, { route: "/admin/pages" });

    fireEvent.click(await screen.findByRole("button", { name: "Validate" }));

    const notContiguous = await screen.findByText("The master and its members are not adjacent.");
    expect(screen.getByText("Already controlled by the Row 1 master.")).toBeInTheDocument();
    // Findings are shown against the item they name — the not_contiguous
    // finding's banner names the group master item it points at (item_id 12).
    expect(within(notContiguous.closest(".banner") as HTMLElement).getByText(/Group · Row 1/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Remove duplicates" }));

    // The duplicated lighting channel item is gone; the group master (and its
    // not_contiguous finding, untouched by this action) remain.
    expect(screen.queryByText(/Lighting · Stage Wash 3/)).not.toBeInTheDocument();
    expect(screen.getByText("The master and its members are not adjacent.")).toBeInTheDocument();
    expect(screen.queryByText("Already controlled by the Row 1 master.")).not.toBeInTheDocument();
  });
});
