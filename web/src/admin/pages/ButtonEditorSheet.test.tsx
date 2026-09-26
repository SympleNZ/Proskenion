/*
 * The panel button sheet (§21.9, docs/admin-screens.html "Pages"): the
 * colour picker is the closed twelve-token group palette, a column outside
 * the panel's width is refused while a large row is not (Q5), and a cell
 * collision with another button in the same panel is refused.
 */
import { fireEvent, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "@/test/render";

import { ButtonEditorSheet } from "./ButtonEditorSheet";
import { GROUP_COLOURS } from "./colours";

const client = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

const RULE = { id: 9, name: "House Full Up", enabled: true };

beforeEach(() => {
  client.api.mockReset();
  client.api.mockImplementation((path: string) => {
    if (path === "/rules") return Promise.resolve({ rules: [RULE] });
    if (path === "/derived-status") return Promise.resolve({ derived_statuses: [] });
    throw new Error(`unhandled request: ${path}`);
  });
});

describe("ButtonEditorSheet", () => {
  it("offers exactly the twelve group-palette tokens, and selecting one toggles it", async () => {
    const onSave = vi.fn();
    renderWithProviders(
      <ButtonEditorSheet open onOpenChange={vi.fn()} initialCol={0} initialRow={0} panelWidth={2} takenCells={new Set()} onSave={onSave} />,
      { route: "/admin/pages" },
    );

    const swatches = await screen.findAllByRole("radio");
    expect(swatches).toHaveLength(GROUP_COLOURS.length);
    expect(GROUP_COLOURS.length).toBe(12);

    const ocean = screen.getByRole("radio", { name: "Ocean" });
    expect(ocean).toHaveAttribute("aria-checked", "false");
    fireEvent.click(ocean);
    expect(ocean).toHaveAttribute("aria-checked", "true");

    fireEvent.change(screen.getByLabelText("Label"), { target: { value: "Go" } });
    fireEvent.change(screen.getByLabelText("Fires rule"), { target: { value: String(RULE.id) } });
    fireEvent.click(screen.getByRole("button", { name: "Add button" }));

    expect(onSave).toHaveBeenCalledWith(expect.objectContaining({ colour: "ocean" }));
  });

  it("refuses a column at or beyond the panel's width", async () => {
    const onSave = vi.fn();
    renderWithProviders(
      <ButtonEditorSheet open onOpenChange={vi.fn()} initialCol={0} initialRow={0} panelWidth={2} takenCells={new Set()} onSave={onSave} />,
      { route: "/admin/pages" },
    );

    fireEvent.change(await screen.findByLabelText("Label"), { target: { value: "Go" } });
    fireEvent.change(screen.getByLabelText("Fires rule"), { target: { value: String(RULE.id) } });
    fireEvent.change(screen.getByLabelText("Column"), { target: { value: "2" } });
    fireEvent.click(screen.getByRole("button", { name: "Add button" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/Column must be between 0 and 1/);
    expect(onSave).not.toHaveBeenCalled();
  });

  it("never refuses a large row — rows are unbounded (Q5)", async () => {
    const onSave = vi.fn();
    renderWithProviders(
      <ButtonEditorSheet open onOpenChange={vi.fn()} initialCol={0} initialRow={0} panelWidth={2} takenCells={new Set()} onSave={onSave} />,
      { route: "/admin/pages" },
    );

    fireEvent.change(await screen.findByLabelText("Label"), { target: { value: "Go" } });
    fireEvent.change(screen.getByLabelText("Fires rule"), { target: { value: String(RULE.id) } });
    fireEvent.change(screen.getByLabelText("Row"), { target: { value: "999" } });
    fireEvent.click(screen.getByRole("button", { name: "Add button" }));

    expect(onSave).toHaveBeenCalledWith(expect.objectContaining({ row: 999 }));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("refuses a cell another button in the panel already occupies", async () => {
    const onSave = vi.fn();
    renderWithProviders(
      <ButtonEditorSheet
        open
        onOpenChange={vi.fn()}
        initialCol={0}
        initialRow={0}
        panelWidth={2}
        takenCells={new Set(["0:0"])}
        onSave={onSave}
      />,
      { route: "/admin/pages" },
    );

    fireEvent.change(await screen.findByLabelText("Label"), { target: { value: "Go" } });
    fireEvent.change(screen.getByLabelText("Fires rule"), { target: { value: String(RULE.id) } });
    fireEvent.click(screen.getByRole("button", { name: "Add button" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Another button already occupies this cell");
    expect(onSave).not.toHaveBeenCalled();
  });
});
