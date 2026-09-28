/*
 * The `?` help sheet (spec §21.24 *Help*, §24.2, §24.3): opens from
 * anywhere with `?` except while typing, is a proper dialog (focus trap,
 * Escape closes it, focus returns to where it was).
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { HelpSheet } from "./HelpSheet";

/** A route each tier can plausibly be on — `HelpContent`'s "On this screen" section reads the current location. */
const ROUTE_FOR_TIER: Record<"admin" | "operator" | "hirer", string> = {
  admin: "/admin/devices",
  operator: "/app/pages",
  hirer: "/hire/1",
};

function renderWithTrigger(tier: "admin" | "operator" | "hirer" = "admin") {
  return render(
    <MemoryRouter initialEntries={[ROUTE_FOR_TIER[tier]]}>
      <button type="button">Before</button>
      <HelpSheet tier={tier} />
    </MemoryRouter>,
  );
}

describe("HelpSheet", () => {
  it("opens on ? and shows the admin's shortcut list, version and recovery summary", async () => {
    renderWithTrigger("admin");
    fireEvent.keyDown(document, { key: "?" });

    const dialog = await screen.findByRole("dialog", { name: "Help" });
    expect(within(dialog).getByText("Keyboard shortcuts")).toBeInTheDocument();
    expect(within(dialog).getByText("Escape")).toBeInTheDocument();
    expect(within(dialog).getByText("Version")).toBeInTheDocument();
    expect(within(dialog).getByText("If this appliance will not start")).toBeInTheDocument();
  });

  it("shows the operator's own content — the quick reference, never the recovery summary (spec §21.24)", async () => {
    renderWithTrigger("operator");
    fireEvent.keyDown(document, { key: "?" });

    const dialog = await screen.findByRole("dialog", { name: "Help" });
    expect(within(dialog).getByRole("heading", { name: "Operator quick reference" })).toBeInTheDocument();
    expect(within(dialog).queryByText("If this appliance will not start")).not.toBeInTheDocument();
  });

  it("shows the hirer's own content — shortcuts and who to ask, never version or docs (spec §21.24)", async () => {
    renderWithTrigger("hirer");
    fireEvent.keyDown(document, { key: "?" });

    const dialog = await screen.findByRole("dialog", { name: "Help" });
    expect(within(dialog).getByText("Who to ask")).toBeInTheDocument();
    expect(within(dialog).queryByText("Version")).not.toBeInTheDocument();
    expect(within(dialog).queryByText("If this appliance will not start")).not.toBeInTheDocument();
  });

  it("does not open while typing in an input", () => {
    render(
      <>
        <input aria-label="Some field" />
        <HelpSheet tier="admin" />
      </>,
    );
    const input = screen.getByLabelText("Some field");
    input.focus();
    fireEvent.keyDown(input, { key: "?" });
    expect(screen.queryByRole("dialog", { name: "Help" })).not.toBeInTheDocument();
  });

  it("does not open while typing in a textarea", () => {
    render(
      <>
        <textarea aria-label="Notes" />
        <HelpSheet tier="admin" />
      </>,
    );
    const textarea = screen.getByLabelText("Notes");
    textarea.focus();
    fireEvent.keyDown(textarea, { key: "?" });
    expect(screen.queryByRole("dialog", { name: "Help" })).not.toBeInTheDocument();
  });

  it("does not open while typing in a contenteditable element", () => {
    render(
      <>
        <div contentEditable data-testid="editable" />
        <HelpSheet tier="admin" />
      </>,
    );
    const editable = screen.getByTestId("editable");
    editable.focus();
    fireEvent.keyDown(editable, { key: "?" });
    expect(screen.queryByRole("dialog", { name: "Help" })).not.toBeInTheDocument();
  });

  it("ignores ? combined with a modifier, so it never steals another shortcut", () => {
    renderWithTrigger();
    fireEvent.keyDown(document, { key: "?", ctrlKey: true });
    expect(screen.queryByRole("dialog", { name: "Help" })).not.toBeInTheDocument();
  });

  it("Escape closes it and returns focus to where it was", async () => {
    renderWithTrigger();
    const events = userEvent.setup();
    const before = screen.getByRole("button", { name: "Before" });
    await events.click(before);
    expect(before).toHaveFocus();

    await events.keyboard("?");
    await screen.findByRole("dialog", { name: "Help" });

    await events.keyboard("{Escape}");

    expect(screen.queryByRole("dialog", { name: "Help" })).not.toBeInTheDocument();
    await waitFor(() => expect(before).toHaveFocus());
  });

  it("traps focus inside the sheet while it is open", async () => {
    renderWithTrigger();
    fireEvent.keyDown(document, { key: "?" });
    const dialog = await screen.findByRole("dialog", { name: "Help" });

    const events = userEvent.setup();
    // Tab all the way around twice — focus must never land on the "Before"
    // button behind the sheet, wherever it started inside.
    for (let i = 0; i < 12; i++) {
      await events.tab();
      expect(dialog.contains(document.activeElement)).toBe(true);
    }
  });
});
