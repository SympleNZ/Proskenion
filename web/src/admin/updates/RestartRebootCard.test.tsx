/*
 * Restart and reboot (spec §21.24, §21.27, contracts §5 `POST
 * /system/restart`, `POST /system/reboot`): each names its consequence
 * before it happens, and each waits for reconnection afterwards.
 */
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/api/client";
import { renderWithProviders } from "@/test/render";

import { RestartRebootCard } from "./RestartRebootCard";

const client = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

beforeEach(() => {
  client.api.mockReset();
  client.api.mockImplementation((path: string) => {
    if (path === "/health") return Promise.resolve({ status: "ok" });
    return Promise.resolve(undefined);
  });
});

describe("RestartRebootCard — restart", () => {
  it("names the consequence before it happens, not just 'are you sure'", async () => {
    renderWithProviders(<RestartRebootCard />, { route: "/admin/updates" });

    fireEvent.click(screen.getByRole("button", { name: "Restart application" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent(/restarts the Proskenion application/);
    expect(dialog).toHaveTextContent(/unavailable for about a minute/);
    expect(dialog).toHaveTextContent(/operating system and the appliance itself are not affected/);
    expect(client.api).not.toHaveBeenCalledWith("/system/restart", expect.anything());
  });

  it("restarts, then waits for reconnection", async () => {
    renderWithProviders(<RestartRebootCard />, { route: "/admin/updates" });

    fireEvent.click(screen.getByRole("button", { name: "Restart application" }));
    const dialog = await screen.findByRole("alertdialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Restart" }));

    await waitFor(() => expect(client.api).toHaveBeenCalledWith("/system/restart", expect.objectContaining({ method: "POST" })));
    expect(await screen.findByText("Restarting the application…")).toBeInTheDocument();
    expect(screen.getByText(/This will take about a minute/)).toBeInTheDocument();
  });

  it("reports a failure inline rather than waiting forever", async () => {
    client.api.mockImplementation((path: string) => {
      if (path === "/system/restart") return Promise.reject(new ApiError(503, "device_unavailable", "The appliance could not restart the application."));
      return Promise.resolve({ status: "ok" });
    });
    renderWithProviders(<RestartRebootCard />, { route: "/admin/updates" });

    fireEvent.click(screen.getByRole("button", { name: "Restart application" }));
    fireEvent.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Restart" }));

    expect(await screen.findByText("The appliance could not restart the application.")).toBeInTheDocument();
  });
});

describe("RestartRebootCard — reboot", () => {
  it("names that the operating system reboots too, and disconnects everyone", async () => {
    renderWithProviders(<RestartRebootCard />, { route: "/admin/updates" });

    fireEvent.click(screen.getByRole("button", { name: "Reboot appliance" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent(/reboots the whole appliance, including the operating system/);
    expect(dialog).toHaveTextContent(/staff and hirers/);
  });

  it("reboots, then waits for reconnection", async () => {
    renderWithProviders(<RestartRebootCard />, { route: "/admin/updates" });

    fireEvent.click(screen.getByRole("button", { name: "Reboot appliance" }));
    const dialog = await screen.findByRole("alertdialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Reboot" }));

    await waitFor(() => expect(client.api).toHaveBeenCalledWith("/system/reboot", expect.objectContaining({ method: "POST" })));
    expect(await screen.findByText("Rebooting the appliance…")).toBeInTheDocument();
  });

  it("is refused while an OS slot is on trial, and says so rather than reconnecting", async () => {
    client.api.mockImplementation((path: string) => {
      if (path === "/system/reboot") {
        return Promise.reject(
          new ApiError(
            422,
            "validation_failed",
            "The appliance cannot reboot while an operating system trial is in progress — a plain reboot would abandon it silently. Use OS roll back instead, which says what it does.",
            { reason: "os_trial", slot: "b", version: "v1.5.0" },
          ),
        );
      }
      return Promise.resolve({ status: "ok" });
    });
    renderWithProviders(<RestartRebootCard />, { route: "/admin/updates" });

    fireEvent.click(screen.getByRole("button", { name: "Reboot appliance" }));
    fireEvent.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Reboot" }));

    expect(await screen.findByText(/cannot reboot while an operating system trial is in progress/)).toBeInTheDocument();
    expect(screen.getByText(/Use OS roll back instead/)).toBeInTheDocument();
    // Refused, not applied — no reconnection wait was started.
    expect(screen.queryByText("Rebooting the appliance…")).not.toBeInTheDocument();
  });
});
