/*
 * The Documentation section (spec §21.24 *Help*): per-tier visibility, the
 * admin's picker-then-reader flow, and the operator's single doc rendering
 * directly with nothing to pick between.
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { DocsSection } from "./DocsSection";

describe("DocsSection — per tier", () => {
  it("renders nothing at all for a hirer", () => {
    const { container } = render(<DocsSection tier="hirer" />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders the operator quick reference directly for an operator — one doc, nothing to pick", async () => {
    render(<DocsSection tier="operator" />);
    expect(screen.getByRole("heading", { name: "Operator quick reference" })).toBeInTheDocument();
    expect(screen.getByText(/Recalling a scene/)).toBeInTheDocument();
    // Nothing to go "back" to with only one document.
    expect(screen.queryByRole("button", { name: /Back to Documentation/ })).not.toBeInTheDocument();
    // None of the admin-only docs leak through.
    expect(screen.queryByText(/Before a hire/)).not.toBeInTheDocument();
  });

  it("lists all four for an admin, picks one, and goes back", async () => {
    const events = userEvent.setup();
    render(<DocsSection tier="admin" />);

    expect(screen.getByRole("button", { name: "Operator quick reference" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Hire handover" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Recovery card" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Accessibility check" })).toBeInTheDocument();

    await events.click(screen.getByRole("button", { name: "Recovery card" }));
    expect(screen.getByRole("heading", { name: "Recovery card" })).toBeInTheDocument();
    expect(screen.getByText(/Is it actually down\?/)).toBeInTheDocument();

    await events.click(screen.getByRole("button", { name: /Back to Documentation/ }));
    expect(screen.getByRole("button", { name: "Hire handover" })).toBeInTheDocument();
  });
});
