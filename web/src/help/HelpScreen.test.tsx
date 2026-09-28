/* Admin → Help (spec §21.24 *Help*): the same content as the ? sheet, as a page. */
import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { renderWithProviders } from "@/test/render";

import { HelpScreen } from "./HelpScreen";

describe("HelpScreen", () => {
  it("shows the shortcuts, version and recovery summary the ? sheet shows", () => {
    // Rendered at its own route (`/admin/help`) — `HelpContent`'s "On this
    // screen" section reads the current location, so this needs a router.
    renderWithProviders(<HelpScreen />, { route: "/admin/help", status: "authenticated", tier: "admin" });
    expect(screen.getByRole("heading", { name: "Help", level: 1 })).toBeInTheDocument();
    expect(screen.getByText("Keyboard shortcuts")).toBeInTheDocument();
    expect(screen.getByText("Version")).toBeInTheDocument();
    expect(screen.getByText("If this appliance will not start")).toBeInTheDocument();
    expect(screen.getByText(/reachable from anywhere.*with the \? key/i)).toBeInTheDocument();
  });
});
