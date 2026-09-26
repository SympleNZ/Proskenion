/*
 * The Markdown renderer (spec §21.24 *Help*). Real-file cases prove the four
 * bundled documents actually render without throwing — the only thing a
 * hand-written parser like this really needs proving against real input —
 * and targeted cases prove each construct in the spec's list individually.
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { DOCS } from "./docs";
import { RenderedMarkdown } from "./markdown";

describe("RenderedMarkdown — the four real bundled files", () => {
  for (const doc of DOCS) {
    it(`renders ${doc.filename} without throwing`, () => {
      const { container } = render(<RenderedMarkdown source={doc.raw} onNavigate={vi.fn()} />);
      expect(container.querySelector(".doc-content")).toBeInTheDocument();
      // Every one of the four has at least one heading and one paragraph.
      expect(container.querySelector("h1, h2, h3, h4, h5, h6")).toBeInTheDocument();
      expect(container.querySelector("p")).toBeInTheDocument();
    });
  }
});

describe("RenderedMarkdown — individual constructs", () => {
  it("headings, by level", () => {
    render(<RenderedMarkdown source={"# One\n\n## Two\n\n### Three"} onNavigate={vi.fn()} />);
    expect(screen.getByRole("heading", { level: 1, name: "One" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 2, name: "Two" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 3, name: "Three" })).toBeInTheDocument();
  });

  it("a paragraph, with bold and italic inline", () => {
    render(<RenderedMarkdown source={"Plain **bold** and *italic* text."} onNavigate={vi.fn()} />);
    const paragraph = screen.getByText(/Plain/);
    expect(paragraph.querySelector("strong")).toHaveTextContent("bold");
    expect(paragraph.querySelector("em")).toHaveTextContent("italic");
  });

  it("inline code", () => {
    render(<RenderedMarkdown source={"Run `npm test` first."} onNavigate={vi.fn()} />);
    expect(screen.getByText("npm test").tagName).toBe("CODE");
  });

  it("a fenced code block", () => {
    const { container } = render(<RenderedMarkdown source={"```\nline one\nline two\n```"} onNavigate={vi.fn()} />);
    expect(container.querySelector("pre code")).toHaveTextContent("line one line two");
  });

  it("a bullet list", () => {
    render(<RenderedMarkdown source={"- First\n- Second"} onNavigate={vi.fn()} />);
    const list = screen.getByRole("list");
    expect(list.tagName).toBe("UL");
    expect(list.children).toHaveLength(2);
  });

  it("a numbered list", () => {
    render(<RenderedMarkdown source={"1. First\n2. Second"} onNavigate={vi.fn()} />);
    expect(screen.getByRole("list").tagName).toBe("OL");
  });

  it("checkboxes, checked and unchecked", () => {
    render(<RenderedMarkdown source={"- [ ] Not done\n- [x] Done"} onNavigate={vi.fn()} />);
    expect(screen.getByText("Unchecked:")).toBeInTheDocument();
    expect(screen.getByText("Checked:")).toBeInTheDocument();
  });

  it("a blockquote", () => {
    const { container } = render(<RenderedMarkdown source={"> For: someone in particular."} onNavigate={vi.fn()} />);
    expect(container.querySelector("blockquote")).toHaveTextContent("For: someone in particular.");
  });

  it("a horizontal rule", () => {
    const { container } = render(<RenderedMarkdown source={"Above\n\n---\n\nBelow"} onNavigate={vi.fn()} />);
    expect(container.querySelector("hr")).toBeInTheDocument();
  });

  it("a table, with an inline-formatted header cell", () => {
    render(<RenderedMarkdown source={"| **Tab** | Meaning |\n|---|---|\n| Pages | The default |"} onNavigate={vi.fn()} />);
    const table = screen.getByRole("table");
    expect(table.querySelector("th strong")).toHaveTextContent("Tab");
    expect(table).toHaveTextContent("The default");
  });

  it("an http(s) link opens in a new tab", () => {
    render(<RenderedMarkdown source={"[the school site](https://school.example.nz)"} onNavigate={vi.fn()} />);
    const link = screen.getByRole("link", { name: "the school site" });
    expect(link).toHaveAttribute("href", "https://school.example.nz");
    expect(link).toHaveAttribute("target", "_blank");
  });

  it("a link to another bundled doc navigates inside the app, never leaving it", async () => {
    const events = userEvent.setup();
    const onNavigate = vi.fn();
    render(<RenderedMarkdown source={"See [the recovery card](recovery-card.md) first."} onNavigate={onNavigate} />);
    const trigger = screen.getByRole("button", { name: "the recovery card" });
    await events.click(trigger);
    expect(onNavigate).toHaveBeenCalledWith("recovery-card");
  });

  it("a link to something not bundled — nowhere to go offline — renders as plain text, not a dead link", () => {
    render(<RenderedMarkdown source={"Full steps: [recovery](docs/hardware/recovery.md)."} onNavigate={vi.fn()} />);
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "recovery" })).not.toBeInTheDocument();
    expect(screen.getByText(/Full steps:/)).toHaveTextContent("Full steps: recovery.");
  });
});
