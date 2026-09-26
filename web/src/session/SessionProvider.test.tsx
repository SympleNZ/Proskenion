/* Session expiry (spec §6.5, §21.8): overlay under 30 minutes, redirect over, "Access updated" for a revoked hirer. */
import { act, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, apiEvents } from "@/api/client";
import { CLOSE_ACCESS_REVOKED, CLOSE_SESSION_EXPIRED, LiveSocket, type SocketLike } from "@/live/socket";
import { renderWithProviders } from "@/test/render";

import { ABSOLUTE_EXPIRY_REASON, REAUTH_WINDOW_MS, decideUnauthenticated } from "./expiry";

const MINUTE = 60_000;

function announceUnauthenticated(reason?: string) {
  const detail = reason ? { reason } : {};
  const error = new ApiError(401, "unauthenticated", "Session ended", detail);
  act(() => {
    apiEvents.dispatchEvent(new CustomEvent("unauthenticated", { detail: error }));
  });
}

/**
 * Close a live socket with `code` as the server would, through the real
 * client: the announcement is the socket's own, not a hand-made event.
 */
function announceSessionEnded(code: number) {
  let fake: SocketLike | null = null;
  const socket = new LiveSocket({
    url: "ws://appliance.test/ws?v=1",
    domains: [],
    createSocket: () => {
      const created: SocketLike = {
        readyState: 0,
        send: () => undefined,
        close: () => undefined,
        onopen: null,
        onclose: null,
        onmessage: null,
        onerror: null,
      };
      fake = created;
      return created;
    },
  });
  socket.start();
  const server = fake as SocketLike | null;
  if (!server) throw new Error("no socket was created");
  server.onclose?.({ code });
}

describe("decideUnauthenticated", () => {
  const now = 1_000_000_000_000;
  const session = (expiresAt: number, tier: "admin" | "operator" | "hirer" = "operator") => ({
    tier,
    expiresAt,
    absoluteExpiresAt: null,
    certificate: "trusted" as const,
  });

  it("gives the overlay when the session expired under 30 minutes ago", () => {
    expect(decideUnauthenticated(session(now - 5 * MINUTE), undefined, now)).toBe("expired");
    expect(decideUnauthenticated(session(now - REAUTH_WINDOW_MS + 1), undefined, now)).toBe("expired");
    expect(decideUnauthenticated(session(now + MINUTE), undefined, now)).toBe("expired");
  });

  it("redirects when expired for 30 minutes or more, or with no session at all", () => {
    expect(decideUnauthenticated(session(now - REAUTH_WINDOW_MS), undefined, now)).toBe("redirect");
    expect(decideUnauthenticated(session(now - 2 * REAUTH_WINDOW_MS), undefined, now)).toBe("redirect");
    expect(decideUnauthenticated(null, undefined, now)).toBe("redirect");
  });

  it("shows Access updated for a revoked hirer regardless of timing", () => {
    expect(decideUnauthenticated(session(now - 3 * REAUTH_WINDOW_MS, "hirer"), "hirer_revoked", now)).toBe("revoked");
    expect(decideUnauthenticated(null, "hirer_revoked", now)).toBe("revoked");
  });

  it("never shows a staff session Access updated", () => {
    expect(decideUnauthenticated(session(now + MINUTE, "admin"), "hirer_revoked", now)).toBe("redirect");
    expect(decideUnauthenticated(session(now + MINUTE, "operator"), "hirer_revoked", now)).toBe("redirect");
  });

  it("sends every tier to the login screen at the absolute expiry, never the overlay", () => {
    for (const tier of ["admin", "operator", "hirer"] as const) {
      expect(decideUnauthenticated(session(now + MINUTE, tier), ABSOLUTE_EXPIRY_REASON, now)).toBe("redirect");
    }
  });
});

describe("SessionProvider", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("shows the re-authentication overlay over the preserved page", () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new Error("offline"));
    renderWithProviders(<div>the page</div>, {
      route: "/app",
      status: "authenticated",
      expiresAt: Date.now() - 5 * MINUTE,
      routes: { "/login": <div>LOGIN</div> },
    });
    announceUnauthenticated();
    expect(screen.getByRole("dialog", { name: "Session expired" })).toBeInTheDocument();
    expect(screen.getByText("the page")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign in again" })).toBeInTheDocument();
    expect(screen.queryByText("LOGIN")).not.toBeInTheDocument();
  });

  it("redirects to /login when the session expired more than 30 minutes ago", () => {
    renderWithProviders(<div>the page</div>, {
      route: "/app",
      status: "authenticated",
      expiresAt: Date.now() - 31 * MINUTE,
      routes: { "/login": <div>LOGIN</div> },
    });
    announceUnauthenticated();
    expect(screen.getByText("LOGIN")).toBeInTheDocument();
    expect(screen.queryByText("the page")).not.toBeInTheDocument();
  });

  it("sends staff to the login screen when the socket reports the absolute expiry", () => {
    renderWithProviders(<div>the page</div>, {
      route: "/app",
      status: "authenticated",
      expiresAt: Date.now() + 20 * MINUTE,
      routes: { "/login": <div>LOGIN</div> },
    });
    act(() => announceSessionEnded(CLOSE_SESSION_EXPIRED));
    expect(screen.getByText("LOGIN")).toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: "Session expired" })).not.toBeInTheDocument();
  });

  it("sends a hirer to the PIN screen when the socket reports the absolute expiry", () => {
    renderWithProviders(<div>the page</div>, {
      route: "/hire",
      status: "authenticated",
      tier: "hirer",
      routes: { "/login": <div>LOGIN</div> },
    });
    act(() => announceSessionEnded(CLOSE_SESSION_EXPIRED));
    expect(screen.getByText("LOGIN")).toBeInTheDocument();
  });

  it("shows a hirer Access updated, with no login prompt, when the socket closes 4003", () => {
    renderWithProviders(<div>the page</div>, {
      route: "/hire",
      status: "authenticated",
      tier: "hirer",
      routes: { "/login": <div>LOGIN</div> },
    });
    act(() => announceSessionEnded(CLOSE_ACCESS_REVOKED));
    expect(screen.getByRole("alertdialog", { name: "Access updated" })).toBeInTheDocument();
    expect(screen.queryByLabelText("Password")).not.toBeInTheDocument();
    expect(screen.queryByText("LOGIN")).not.toBeInTheDocument();
  });

  it("shows Access updated with no login prompt for a revoked hirer", () => {
    renderWithProviders(<div>the page</div>, {
      route: "/hire",
      status: "authenticated",
      tier: "hirer",
      routes: { "/login": <div>LOGIN</div> },
    });
    announceUnauthenticated("hirer_revoked");
    expect(screen.getByRole("alertdialog", { name: "Access updated" })).toBeInTheDocument();
    expect(screen.getByText(/updated by venue staff/)).toBeInTheDocument();
    expect(screen.queryByLabelText("Password")).not.toBeInTheDocument();
  });
});
