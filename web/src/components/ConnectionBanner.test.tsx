/*
 * The outage states (spec §21.27, §16.8): the reconnecting banner carries the
 * cached label, and close 4001 reads "Refresh required", not "Connection lost".
 */
import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { REFRESH_REQUIRED_MESSAGE, setConnectionFault } from "@/live/connection";
import { setConnectionState } from "@/live/store";

import { ConnectionBanner } from "./ConnectionBanner";

afterEach(() => {
  act(() => {
    setConnectionFault(null);
    setConnectionState("connected");
  });
});

describe("ConnectionBanner", () => {
  it("shows nothing while connected", () => {
    const { container } = render(<ConnectionBanner />);
    expect(container).toBeEmptyDOMElement();
  });

  it("labels the values on screen as cached while reconnecting", () => {
    act(() => setConnectionState("reconnecting"));
    render(<ConnectionBanner />);
    expect(screen.getByText(/Reconnecting to the controller/)).toBeInTheDocument();
    expect(screen.getByText("Cached values")).toBeInTheDocument();
  });

  it("presents close 4001 as Refresh required, with a Refresh button", () => {
    act(() => {
      setConnectionFault("refresh_required");
      setConnectionState("lost");
    });
    render(<ConnectionBanner />);
    expect(screen.getByRole("alertdialog", { name: "Refresh required" })).toBeInTheDocument();
    expect(screen.getByText(REFRESH_REQUIRED_MESSAGE)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Refresh" })).toBeInTheDocument();
    expect(screen.queryByText("Connection lost")).not.toBeInTheDocument();
  });

  it("keeps the cached label on the connection-lost overlay", () => {
    act(() => setConnectionState("lost"));
    render(<ConnectionBanner />);
    expect(screen.getByRole("alertdialog", { name: "Connection lost" })).toBeInTheDocument();
    expect(screen.getByText("Cached values")).toBeInTheDocument();
  });
});
