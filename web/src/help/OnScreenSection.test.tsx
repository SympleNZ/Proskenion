/*
 * The "On this screen" section (spec §21.24 *Help*, Simon's 28 Sep request):
 * the right summary and entries for the current route, tier filtering, and
 * the "Read more" link into the bundled docs. `onScreen.test.ts` covers the
 * DOM-derivation and route-resolution logic directly; this covers it as
 * rendered.
 */
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { HelpButton } from "./HelpButton";
import { OnScreenSection } from "./OnScreenSection";

/**
 * `#main` (`shells/Shell.tsx`) is where `OnScreenSection` looks for the
 * screen's own inline help — `screenContent` stands in for whatever the
 * routed screen has actually rendered there.
 */
function renderOnScreen(route: string, tier: "admin" | "operator" | "hirer", screenContent: ReactNode = null, onOpenDoc = vi.fn()) {
  render(
    <MemoryRouter initialEntries={[route]}>
      <div id="main">{screenContent}</div>
      <OnScreenSection tier={tier} onOpenDoc={onOpenDoc} />
    </MemoryRouter>,
  );
  return { onOpenDoc };
}

describe("OnScreenSection — three different routes", () => {
  it("admin devices: the Devices summary, and its two inline help entries in the order they appear", () => {
    renderOnScreen(
      "/admin/devices",
      "admin",
      <>
        <HelpButton id="devices.category" />
        <HelpButton id="devices.name" />
      </>,
    );

    const section = screen.getByRole("heading", { name: "On this screen" }).closest("section");
    expect(section).not.toBeNull();
    expect(within(section as HTMLElement).getByText(/drivers for every piece of connected hardware/)).toBeInTheDocument();

    const terms = within(section as HTMLElement)
      .getAllByRole("term")
      .map((dt) => dt.textContent);
    expect(terms).toEqual(["What is it", "Name"]);
  });

  it("operator mixer: the Mixer summary, no entries (none rendered here yet), and a link into the docs", async () => {
    const events = userEvent.setup();
    const { onOpenDoc } = renderOnScreen("/app/mixer", "operator");

    const section = screen.getByRole("heading", { name: "On this screen" }).closest("section") as HTMLElement;
    expect(within(section).getByText(/virtual channel strips for the mixer/)).toBeInTheDocument();
    expect(within(section).queryAllByRole("term")).toEqual([]);

    await events.click(within(section).getByRole("button", { name: "Read more in the documentation" }));
    expect(onOpenDoc).toHaveBeenCalledWith("operator-quick-reference", "The screens");
  });

  it("hirer page: the fixed hirer summary, and no documentation link", () => {
    renderOnScreen("/hire/42", "hirer");

    const section = screen.getByRole("heading", { name: "On this screen" }).closest("section") as HTMLElement;
    expect(within(section).getByText(/your assigned page/i)).toBeInTheDocument();
    expect(within(section).queryByRole("button", { name: "Read more in the documentation" })).not.toBeInTheDocument();
  });
});

describe("OnScreenSection — a specific screen's own doc section, not just the generic one", () => {
  it("operator scenes links to \"Recalling a scene\", not the generic screens table", async () => {
    const events = userEvent.setup();
    const { onOpenDoc } = renderOnScreen("/app/scenes", "operator");
    await events.click(screen.getByRole("button", { name: "Read more in the documentation" }));
    expect(onOpenDoc).toHaveBeenCalledWith("operator-quick-reference", "Recalling a scene");
  });
});

describe("OnScreenSection — tier filtering", () => {
  it("renders nothing for a hirer on an admin path — a combination routing itself never produces, checked anyway", () => {
    renderOnScreen("/admin/devices", "hirer");
    expect(screen.queryByRole("heading", { name: "On this screen" })).not.toBeInTheDocument();
  });

  it("renders nothing for an operator on a /hire path", () => {
    renderOnScreen("/hire/1", "operator");
    expect(screen.queryByRole("heading", { name: "On this screen" })).not.toBeInTheDocument();
  });

  it("renders nothing for an admin or an operator on an unknown admin segment", () => {
    renderOnScreen("/admin/not-a-real-screen", "admin");
    expect(screen.queryByRole("heading", { name: "On this screen" })).not.toBeInTheDocument();
  });
});
