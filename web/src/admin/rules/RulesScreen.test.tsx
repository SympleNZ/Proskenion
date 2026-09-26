/*
 * The Rules screen's tab switching (spec §21.17, §8.10): the derived-status
 * monitor connects only while its tab is showing, and disconnects the
 * moment the Rules tab is chosen instead — nothing is left open behind it.
 */
import { fireEvent, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "@/test/render";

import { RulesScreen } from "./RulesScreen";

const client = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

class MockEventSource {
  static instances: MockEventSource[] = [];
  closed = false;
  constructor() {
    MockEventSource.instances.push(this);
  }
  addEventListener() {
    /* no events emitted — this test only checks connect/close bookkeeping */
  }
  removeEventListener() {}
  close() {
    this.closed = true;
  }
}

describe("RulesScreen tabs", () => {
  beforeEach(() => {
    client.api.mockReset();
    client.api.mockImplementation((path: string) => {
      if (path === "/rules") return Promise.resolve({ rules: [] });
      if (path === "/rules/state") return Promise.resolve({ external_control: false, rules: [] });
      if (path === "/rules/log") return Promise.resolve({ entries: [] });
      if (path === "/derived-status") return Promise.resolve({ derived_statuses: [] });
      if (path === "/derived-status/state") return Promise.resolve({ statuses: [] });
      if (path === "/knx/addresses") return Promise.resolve([]);
      if (path === "/scenes") return Promise.resolve({ scenes: [] });
      if (path === "/lighting/groups") return Promise.resolve({ groups: [] });
      if (path === "/devices") return Promise.resolve({ devices: [] });
      return Promise.resolve({});
    });
    MockEventSource.instances = [];
    globalThis.EventSource = MockEventSource as unknown as typeof EventSource;
  });

  it("opens the monitor's connection only on the Derived status tab, and closes it on leaving", async () => {
    renderWithProviders(<RulesScreen />, { route: "/admin/rules", tier: "admin", status: "authenticated" });

    // The Rules tab is the default — no server-sent events connection yet.
    expect(MockEventSource.instances).toHaveLength(0);

    fireEvent.click(await screen.findByRole("tab", { name: "Derived status" }));
    expect(MockEventSource.instances).toHaveLength(1);
    const source = MockEventSource.instances[0]!;
    expect(source.closed).toBe(false);

    fireEvent.click(await screen.findByRole("tab", { name: "Rules" }));
    expect(source.closed).toBe(true);

    // Returning to the Derived status tab opens a fresh connection, not a leaked second one.
    fireEvent.click(await screen.findByRole("tab", { name: "Derived status" }));
    expect(MockEventSource.instances).toHaveLength(2);
    expect(MockEventSource.instances[1]!.closed).toBe(false);
  });
});
