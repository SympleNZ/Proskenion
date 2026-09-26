/* Admin navigation (spec §21.6): the sidebar renders the four sections and their items in order. */
import { screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { NotPermittedPage } from "@/pages/Placeholders";
import { RequireTier } from "@/routes/guards";
import { renderWithProviders } from "@/test/render";

const client = vi.hoisted(() => ({ api: vi.fn() }));
vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

import { AdminShell } from "./AdminShell";

function serveDevices(devices: readonly { category: string }[] = []): void {
  client.api.mockImplementation((path: string) => {
    if (path === "/devices") return Promise.resolve({ devices });
    return Promise.resolve({});
  });
}

const WITHOUT_CONTROL_SURFACE = [
  "Pages",
  "Scenes",
  "Rules",
  "Hirer Access",
  "KNX Library",
  "Lighting",
  "Mixer",
  "HDMI",
  "Network",
  "Certificates",
  "Devices",
  "Email",
  "Backup",
  "Updates",
  "Health",
  "Logs",
  "Users",
  "Help",
];

const WITH_CONTROL_SURFACE = [
  "Pages",
  "Scenes",
  "Rules",
  "Hirer Access",
  "KNX Library",
  "Lighting",
  "Mixer",
  "HDMI",
  "Control Surface",
  "Network",
  "Certificates",
  "Devices",
  "Email",
  "Backup",
  "Updates",
  "Health",
  "Logs",
  "Users",
  "Help",
];

/** Flushes the `/devices` query's microtasks so the nav has settled before assertions. */
async function settle(): Promise<void> {
  await screen.findByRole("navigation", { name: "Admin" });
  await new Promise((resolve) => setTimeout(resolve, 0));
}

describe("AdminShell", () => {
  beforeEach(() => {
    client.api.mockReset();
    serveDevices();
  });

  it("renders the §21.6 sidebar items in order under their sections, with Control Surface absent when none is configured", async () => {
    renderWithProviders(<AdminShell />, { route: "/admin/scenes", path: "/admin", nested: true, status: "authenticated", tier: "admin" });
    await settle();
    // Scoped to the ADMIN_NAV landmark rather than the whole sidebar: the
    // sidebar also carries the "Main interface" link (below), which is not
    // one of ADMIN_NAV's own sections.
    const nav = screen.getByRole("navigation", { name: "Admin" });
    expect(within(nav).getAllByRole("link").map((a) => a.textContent)).toEqual(WITHOUT_CONTROL_SURFACE);
    const groups = Array.from(nav.querySelectorAll(".nav-group-label")).map((el) => el.textContent);
    expect(groups).toEqual(["Control", "Configure", "System", "Account"]);
    expect(within(nav).getByRole("link", { name: "Scenes" })).toHaveAttribute("aria-current", "page");
  });

  it("shows Control Surface once a control-surface device is configured (spec §21.25)", async () => {
    serveDevices([{ category: "control_surface" }]);
    renderWithProviders(<AdminShell />, { route: "/admin/scenes", path: "/admin", nested: true, status: "authenticated", tier: "admin" });
    await settle();
    const nav = screen.getByRole("navigation", { name: "Admin" });
    expect(within(nav).getAllByRole("link").map((a) => a.textContent)).toEqual(WITH_CONTROL_SURFACE);
  });

  it("links back to the main interface, so admin is never a dead end without editing the URL (25 Sep 2026, v0.1.2)", () => {
    renderWithProviders(<AdminShell />, { route: "/admin/scenes", path: "/admin", nested: true, status: "authenticated", tier: "admin" });
    const links = screen.getAllByRole("link", { name: "Main interface" });
    expect(links.length).toBeGreaterThan(0);
    for (const link of links) {
      expect(link).toHaveAttribute("href", "/app");
    }
  });

  it("gives keyboard users a skip link past the sidebar, to a focusable main landmark (§24.7)", () => {
    renderWithProviders(<AdminShell />, { route: "/admin/scenes", path: "/admin", nested: true, status: "authenticated", tier: "admin" });
    const skip = screen.getByRole("link", { name: "Skip to main content" });
    expect(skip).toHaveAttribute("href", "#main");
    const main = document.getElementById("main");
    expect(main).not.toBeNull();
    // In the tab order (not -1): `.shell-main` is the shell's own scroll
    // container, and a screen with no focusable content of its own (Health,
    // Help) still needs a keyboard-reachable way to scroll it (axe's
    // `scrollable-region-focusable`, real-browser-only — tests/e2e/accessibility.spec.ts).
    expect(main).toHaveAttribute("tabIndex", "0");
  });

  it("keeps the status bar as the last child", () => {
    renderWithProviders(<AdminShell />, { route: "/admin/scenes", path: "/admin", nested: true, status: "authenticated", tier: "admin" });
    expect(screen.getByTestId("shell").lastElementChild).toBe(screen.getByTestId("status-bar"));
  });

  it("shows the not-permitted state to an operator", () => {
    renderWithProviders(
      <RequireTier allow={["admin"]} denied={<NotPermittedPage />}>
        <AdminShell />
      </RequireTier>,
      { route: "/admin/scenes", path: "/admin", nested: true, status: "authenticated", tier: "operator" },
    );
    expect(screen.getByText("Not permitted")).toBeInTheDocument();
    expect(screen.queryByTestId("admin-sidebar")).not.toBeInTheDocument();
  });

  it("sends a hirer home rather than exposing /admin", () => {
    renderWithProviders(
      <RequireTier allow={["admin"]} denied={<NotPermittedPage />}>
        <AdminShell />
      </RequireTier>,
      { route: "/admin/scenes", path: "/admin", nested: true, status: "authenticated", tier: "hirer", routes: { "/hire": <div>HIRE</div> } },
    );
    expect(screen.getByText("HIRE")).toBeInTheDocument();
  });

  it("redirects anonymous visitors to /login", () => {
    renderWithProviders(
      <RequireTier allow={["admin"]}>
        <AdminShell />
      </RequireTier>,
      { route: "/admin/scenes", path: "/admin", nested: true, status: "anonymous", routes: { "/login": <div>LOGIN</div> } },
    );
    expect(screen.getByText("LOGIN")).toBeInTheDocument();
  });
});
