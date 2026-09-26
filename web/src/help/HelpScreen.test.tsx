/* Admin → Help (spec §21.24 *Help*): the same content as the ? sheet, as a page. */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { HelpScreen } from "./HelpScreen";

describe("HelpScreen", () => {
  it("shows the shortcuts, version and recovery summary the ? sheet shows", () => {
    render(<HelpScreen />);
    expect(screen.getByRole("heading", { name: "Help", level: 1 })).toBeInTheDocument();
    expect(screen.getByText("Keyboard shortcuts")).toBeInTheDocument();
    expect(screen.getByText("Version")).toBeInTheDocument();
    expect(screen.getByText("If this appliance will not start")).toBeInTheDocument();
    expect(screen.getByText(/reachable from anywhere.*with the \? key/i)).toBeInTheDocument();
  });
});
