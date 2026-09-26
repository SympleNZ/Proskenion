/*
 * The Devices screen and its §21.27 states: a skeleton while loading, an
 * error state that says what failed and offers the retry the code calls for,
 * and an empty state with an action the admin tier can actually resolve.
 */
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/api/client";
import { describeMissing, findMissingHelp } from "@/help/coverage";
import { renderWithProviders } from "@/test/render";

import { DevicesScreen } from "./DevicesScreen";
import { DEVICE, FABRICATED_DRIVER } from "./fixtures";

const client = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

function serve(devices: unknown[]) {
  client.api.mockImplementation((path: string) => {
    if (path === "/drivers") return Promise.resolve({ drivers: [FABRICATED_DRIVER] });
    if (path === "/devices") return Promise.resolve({ devices });
    if (path.endsWith("/capabilities")) {
      return Promise.resolve({ category: "video_matrix", as_connected: false, capabilities: FABRICATED_DRIVER.capabilities });
    }
    return Promise.resolve({});
  });
}

describe("DevicesScreen", () => {
  beforeEach(() => {
    client.api.mockReset();
  });

  it("shows a skeleton while loading rather than a spinner", () => {
    serve([]);
    renderWithProviders(<DevicesScreen />, { route: "/admin/devices" });
    expect(screen.getByLabelText("Loading the configured devices")).toBeInTheDocument();
  });

  it("offers to add the first device when none is configured", async () => {
    serve([]);
    renderWithProviders(<DevicesScreen />, { route: "/admin/devices" });

    expect(await screen.findByText("No devices configured")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Add the first device" }));
    expect(await screen.findByRole("dialog", { name: /Add a device/ })).toBeInTheDocument();
  });

  it("groups one card per configured driver instance under what it is", async () => {
    serve([DEVICE]);
    renderWithProviders(<DevicesScreen />, { route: "/admin/devices" });

    expect(await screen.findByRole("heading", { name: "Video matrix", level: 2 })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Fabricated device (test only)", level: 3 })).toBeInTheDocument();
    expect(screen.getByText("fabricated")).toBeInTheDocument();
    expect(screen.getByLabelText("House matrix: Connected")).toBeInTheDocument();
  });

  it("says what failed and offers a retry when the screen cannot render", async () => {
    client.api.mockRejectedValue(new ApiError(500, "internal_error", "The server returned an error"));
    renderWithProviders(<DevicesScreen />, { route: "/admin/devices" });

    expect(await screen.findByText("Could not load the devices")).toBeInTheDocument();
    expect(screen.getByText("500 internal_error")).toBeInTheDocument();

    client.api.mockReset();
    serve([]);
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(screen.getByText("No devices configured")).toBeInTheDocument());
  });

  it("gives every field and primary action help (spec §19.1)", async () => {
    serve([DEVICE]);
    renderWithProviders(<DevicesScreen />, { route: "/admin/devices" });
    await screen.findByRole("heading", { name: "Fabricated device (test only)", level: 3 });

    fireEvent.click(screen.getByRole("button", { name: "Add a device" }));
    await screen.findByRole("dialog", { name: /Add a device/ });

    const missing = findMissingHelp(document.body);
    expect(missing, describeMissing(missing)).toEqual([]);
  });
});
