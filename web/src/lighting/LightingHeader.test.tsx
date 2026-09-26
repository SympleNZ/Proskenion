/*
 * The pinned header's External control toggle (spec §21.11, §7.2.7, §22.3):
 * the LED treatment, and disabled with a tooltip while frames are arriving —
 * detection wins and the toggle cannot force the state off from here.
 */
import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { resetLiveState, setExternalControl } from "@/live/store";
import { renderWithProviders } from "@/test/render";

import { LightingHeader } from "./LightingHeader";

const client = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

beforeEach(() => {
  resetLiveState();
  client.api.mockReset();
  client.api.mockResolvedValue(undefined);
});

function Header() {
  return <LightingHeader fadeSeconds={2} onFadeSecondsChange={vi.fn()} />;
}

describe("LightingHeader — the External control toggle", () => {
  it("is unlit and enabled when off, and turns manual on without a confirmation", async () => {
    renderWithProviders(<Header />);
    const toggle = screen.getByRole("button", { name: "External control" });
    expect(toggle).toHaveAttribute("data-active", "false");
    expect(toggle).not.toBeDisabled();

    fireEvent.click(toggle);
    await waitFor(() => {
      expect(client.api).toHaveBeenCalledWith("/lighting/external-control", { method: "POST", body: { manual: true } });
    });
  });

  it("is disabled with a tooltip while frames are arriving — detection wins (§7.2.7)", () => {
    renderWithProviders(<Header />);
    act(() => setExternalControl("detected"));
    const toggle = screen.getByRole("button", { name: "External control" });
    expect(toggle).toBeDisabled();
    expect(toggle.getAttribute("title")).toMatch(/sending|arriving/i);
  });

  it("is lit while manual, and confirms once before turning off", async () => {
    renderWithProviders(<Header />);
    act(() => setExternalControl("manual"));
    const toggle = screen.getByRole("button", { name: "External control" });
    expect(toggle).toHaveAttribute("data-active", "true");
    expect(toggle).not.toBeDisabled();

    fireEvent.click(toggle);
    // No REST call yet — the confirmation has not been answered.
    expect(client.api).not.toHaveBeenCalled();
    expect(screen.getByRole("alertdialog", { name: /resume controller output/i })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Resume" }));
    await waitFor(() => {
      expect(client.api).toHaveBeenCalledWith("/lighting/external-control", { method: "POST", body: { manual: false } });
    });
  });
});

describe("LightingHeader — Save look (§16.5 admin only, §21.11 drawn in this view)", () => {
  it("is enabled for an admin session and calls the endpoint", async () => {
    renderWithProviders(<Header />, { status: "authenticated", tier: "admin" });
    const button = screen.getByRole("button", { name: "Save look" });
    expect(button).not.toBeDisabled();

    fireEvent.click(button);
    await waitFor(() => {
      expect(client.api).toHaveBeenCalledWith("/lighting/snapshot", { method: "POST" });
    });
  });

  it("is disabled for an operator session", () => {
    renderWithProviders(<Header />, { status: "authenticated", tier: "operator" });
    const button = screen.getByRole("button", { name: "Save look" });
    expect(button).toBeDisabled();

    fireEvent.click(button);
    expect(client.api).not.toHaveBeenCalledWith("/lighting/snapshot", expect.anything());
  });
});
