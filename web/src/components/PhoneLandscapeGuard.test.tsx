/*
 * `PhoneLandscapeGuard` — the DOM shape (spec-free, the owner's decision;
 * `lib/phoneLandscape.ts`'s doc comment). Its actual show/hide is a CSS
 * media query jsdom cannot evaluate (no layout engine) — that is proven in
 * a real browser instead (`tests/e2e/portrait-only.spec.ts`). This is the
 * accessibility contract: an `alertdialog` with a real accessible name, an
 * icon hidden from assistive tech, and no buttons — there is nothing to do
 * but rotate the phone back.
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { PhoneLandscapeGuard } from "./PhoneLandscapeGuard";

describe("PhoneLandscapeGuard", () => {
  it("is an alertdialog named by its own message, with no buttons", () => {
    render(<PhoneLandscapeGuard />);
    const dialog = screen.getByRole("alertdialog", { name: "Turn your phone upright — Proskenion works in portrait on phones." });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(screen.queryAllByRole("button")).toHaveLength(0);
  });

  it("hides its icon from assistive tech", () => {
    const { container } = render(<PhoneLandscapeGuard />);
    const icon = container.querySelector(".phone-landscape-guard-icon");
    expect(icon).toHaveAttribute("aria-hidden", "true");
  });
});
