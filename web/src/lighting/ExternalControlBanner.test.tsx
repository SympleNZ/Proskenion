/*
 * The external-control banner's three states (spec §21.11, §7.2.7): nothing
 * while off, the detected banner with no resume action, and the manual
 * banner with a confirmed Resume.
 */
import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { resetLiveState, setExternalControl } from "@/live/store";
import { renderWithProviders } from "@/test/render";

import { ExternalControlBanner } from "./ExternalControlBanner";

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

describe("ExternalControlBanner — the three states render as §21.11 describes", () => {
  it("renders nothing while off", () => {
    const { container } = renderWithProviders(<ExternalControlBanner />);
    expect(container).toBeEmptyDOMElement();
  });

  it("detected: booth desk banner, no resume action — there is nothing to resume", () => {
    renderWithProviders(<ExternalControlBanner />);
    act(() => setExternalControl("detected"));
    expect(screen.getByText(/under external control/i)).toBeInTheDocument();
    expect(screen.getByText(/showing live fixture levels from the desk/i)).toBeInTheDocument();
    expect(screen.getByText(/stage banks are locked out/i)).toBeInTheDocument();
    expect(screen.getByText(/house lighting is unaffected/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /resume/i })).not.toBeInTheDocument();
  });

  it("manual: DMX suspended banner with a confirmed Resume", async () => {
    renderWithProviders(<ExternalControlBanner />);
    act(() => setExternalControl("manual"));
    expect(screen.getByText(/dmx output suspended/i)).toBeInTheDocument();
    expect(screen.getByText(/levels shown are the controller/i)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /resume controller output/i }));
    expect(client.api).not.toHaveBeenCalled(); // confirms once first

    fireEvent.click(screen.getByRole("button", { name: "Resume" }));
    await waitFor(() => {
      expect(client.api).toHaveBeenCalledWith("/lighting/external-control", { method: "POST", body: { manual: false } });
    });
  });
});
