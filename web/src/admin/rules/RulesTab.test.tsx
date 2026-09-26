/*
 * The Rules tab (spec §21.17): Fire and Test call the right endpoint, and a
 * delete refused with 409 in_use shows what references the rule.
 */
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/api/client";
import { renderWithProviders } from "@/test/render";

import { KNX_ADDRESSES, RULE_BINDING, RULE_SCHEDULE, SCENES } from "./fixtures";
import { RulesTab } from "./RulesTab";

const client = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

const RULES_RESPONSE = { rules: [RULE_BINDING, RULE_SCHEDULE] };
const RULE_STATE_RESPONSE = {
  external_control: false,
  rules: [
    { id: RULE_BINDING.id, name: RULE_BINDING.name, enabled: true, trigger_type: "knx", action_type: "lighting_group", state: true, suppressed: false, fires_automatically: true, note: null, last_result: null, last_fired_at: null, next_fire_at: null },
    { id: RULE_SCHEDULE.id, name: RULE_SCHEDULE.name, enabled: true, trigger_type: "schedule", action_type: "run_scene", state: null, suppressed: false, fires_automatically: true, note: null, last_result: null, last_fired_at: null, next_fire_at: "2026-09-26T23:00:00+12:00" },
  ],
};

function route(handlers: Record<string, (options?: { body?: unknown }) => unknown>) {
  client.api.mockImplementation((path: string, options?: { body?: unknown }) => {
    for (const [prefix, handler] of Object.entries(handlers)) {
      if (path === prefix) return Promise.resolve(handler(options));
    }
    return Promise.resolve({});
  });
}

function defaultRoutes(overrides: Record<string, (options?: { body?: unknown }) => unknown> = {}) {
  route({
    "/rules": () => RULES_RESPONSE,
    "/rules/state": () => RULE_STATE_RESPONSE,
    "/rules/log": () => ({ entries: [] }),
    "/knx/addresses": () => KNX_ADDRESSES,
    "/scenes": () => ({ scenes: SCENES }),
    "/lighting/groups": () => ({ groups: [] }),
    "/devices": () => ({ devices: [] }),
    ...overrides,
  });
}

describe("RulesTab", () => {
  beforeEach(() => {
    client.api.mockReset();
  });

  it("fires a rule as if triggered", async () => {
    defaultRoutes({
      "/rules/1/fire": () => ({ rule_id: 1, triggered_by: "api:admin", fired: true, guard_result: null, result: "started", detail: {} }),
    });
    renderWithProviders(<RulesTab visible={true} />, { route: "/admin/rules", tier: "admin", status: "authenticated" });

    const fireButtons = await screen.findAllByRole("button", { name: "Fire" });
    fireEvent.click(fireButtons[0]!);

    await waitFor(() => expect(client.api).toHaveBeenCalledWith("/rules/1/fire", expect.objectContaining({ body: { value: undefined } })));
  });

  it("tests a rule and waits for the outcome, even though the row shows it is enabled", async () => {
    defaultRoutes({
      "/rules/1/test": () => ({ rule_id: 1, triggered_by: "api:admin", fired: true, guard_result: "passed", result: "ok", detail: {} }),
    });
    renderWithProviders(<RulesTab visible={true} />, { route: "/admin/rules", tier: "admin", status: "authenticated" });

    const testButtons = await screen.findAllByRole("button", { name: "Test" });
    fireEvent.click(testButtons[0]!);

    await waitFor(() => expect(client.api).toHaveBeenCalledWith("/rules/1/test", expect.objectContaining({ body: { value: undefined } })));
  });

  it("shows a schedule row's next time from the scheduler, and no not-firing note", async () => {
    defaultRoutes();
    renderWithProviders(<RulesTab visible={true} />, { route: "/admin/rules", tier: "admin", status: "authenticated" });
    const next = await screen.findByText("26 Sept, 23:00");
    expect(next.closest("time")).toHaveAttribute("dateTime", "2026-09-26T23:00:00+12:00");
    expect(screen.queryByText(/does not fire/)).not.toBeInTheDocument();
  });

  it("shows what references a rule when its delete is refused with 409 in_use", async () => {
    defaultRoutes({
      "/rules/1": () =>
        Promise.reject(
          new ApiError(409, "in_use", "This rule is assigned to a button and cannot be removed", {
            references: [{ entity: "page_buttons", id: 9, name: "Stage panel – button 3" }],
          }),
        ),
    });
    renderWithProviders(<RulesTab visible={true} />, { route: "/admin/rules", tier: "admin", status: "authenticated" });

    const deleteButtons = await screen.findAllByRole("button", { name: "Delete" });
    fireEvent.click(deleteButtons[0]!);

    const confirmDialog = await screen.findByRole("alertdialog");
    fireEvent.click(within(confirmDialog).getByRole("button", { name: "Delete" }));

    expect(await screen.findByText("Stage panel – button 3")).toBeInTheDocument();
    expect(screen.getByText("This rule is assigned to a button and cannot be removed")).toBeInTheDocument();
  });
});
