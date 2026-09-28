/* Login (spec §21.8): rate-limit countdown re-enables at zero; errors preserve or clear exactly as specified. */
import { act, fireEvent, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/api/client";
import { renderWithProviders } from "@/test/render";
import { BUILD_VERSION } from "@/version/buildVersion";

import { HIRER_DISABLED_MESSAGE, LoginPage } from "./LoginPage";

const auth = vi.hoisted(() => ({
  login: vi.fn(),
  hirerLogin: vi.fn(),
  getSession: vi.fn(),
  logout: vi.fn(),
}));

vi.mock("@/api/auth", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/auth")>()),
  login: auth.login,
  hirerLogin: auth.hirerLogin,
  getSession: auth.getSession,
  logout: auth.logout,
}));

const rateLimited = (seconds: number) => new ApiError(429, "rate_limited", "Too many attempts", { retry_after: seconds });
const wrong = () => new ApiError(401, "unauthenticated", "Incorrect");

async function typePin(pin: string) {
  const boxes = screen.getAllByRole("textbox", { name: /PIN digit/ });
  for (const [i, d] of pin.split("").entries()) {
    fireEvent.change(boxes[i]!, { target: { value: d } });
  }
}

describe("LoginPage", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.clearAllMocks();
  });

  it("replaces the Sign in label with a live countdown and re-enables at zero", async () => {
    auth.login.mockRejectedValueOnce(rateLimited(3));
    renderWithProviders(<LoginPage />, { route: "/login" });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "secret" } });
    fireEvent.click(screen.getByTestId("sign-in"));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    const button = screen.getByTestId("sign-in");
    expect(button).toBeDisabled();
    expect(button).toHaveTextContent("Sign in — try again in 0:03");

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(screen.getByTestId("sign-in")).toHaveTextContent("Sign in — try again in 0:02");

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2100);
    });
    expect(screen.getByTestId("sign-in")).toBeEnabled();
    expect(screen.getByTestId("sign-in")).toHaveTextContent(/^Sign in$/);
    // The field value survived the whole time (§21.27: forms never clear on error).
    expect(screen.getByLabelText("Password")).toHaveValue("secret");
  });

  it("keeps the password on a wrong password and announces the error", async () => {
    auth.login.mockRejectedValueOnce(wrong());
    renderWithProviders(<LoginPage />, { route: "/login" });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "nope" } });
    fireEvent.click(screen.getByTestId("sign-in"));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    const staffForm = screen.getByRole("form", { name: "Staff sign in" });
    expect(within(staffForm).getByRole("alert")).toHaveTextContent("Incorrect password");
    expect(screen.getByLabelText("Password")).toHaveValue("nope");
  });

  it("shakes then clears a wrong PIN and leaves the staff field alone", async () => {
    auth.hirerLogin.mockRejectedValueOnce(wrong());
    renderWithProviders(<LoginPage />, { route: "/login" });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "typed" } });
    await typePin("123456");
    expect(auth.hirerLogin).toHaveBeenCalledWith("123456");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(screen.getByTestId("pin-boxes")).toHaveClass("is-shaking");
    expect(screen.getAllByRole("alert").some((el) => el.textContent?.includes("Incorrect PIN"))).toBe(true);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(500);
    });
    expect(screen.getByTestId("pin-boxes")).not.toHaveClass("is-shaking");
    screen.getAllByRole("textbox", { name: /PIN digit/ }).forEach((box) => expect(box).toHaveValue(""));
    expect(screen.getByLabelText("Password")).toHaveValue("typed");
  });

  it("shows the hirer-disabled message verbatim", async () => {
    auth.hirerLogin.mockRejectedValueOnce(new ApiError(403, "permission_denied", "Disabled", { reason: "hirer_disabled" }));
    renderWithProviders(<LoginPage />, { route: "/login" });
    await typePin("111111");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(screen.getByRole("status")).toHaveTextContent(HIRER_DISABLED_MESSAGE);
  });

  it("shows the build version discreetly, below both forms", () => {
    renderWithProviders(<LoginPage />, { route: "/login" });
    expect(screen.getByText(`Proskenion v${BUILD_VERSION}`)).toBeInTheDocument();
  });

  it("redirects by tier after a successful sign-in", async () => {
    auth.login.mockResolvedValueOnce({ tier: "admin", expires_at: new Date(Date.now() + 60_000).toISOString() });
    auth.getSession.mockResolvedValue({
      tier: "admin",
      expires_at: new Date(Date.now() + 60_000).toISOString(),
      absolute_expires_at: new Date(Date.now() + 60_000).toISOString(),
      server_time: new Date().toISOString(),
      certificate: "trusted",
    });
    renderWithProviders(<LoginPage />, { route: "/login", routes: { "/app": <div>OPERATOR</div>, "/hire": <div>HIRE</div> } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "pw" } });
    fireEvent.click(screen.getByTestId("sign-in"));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(screen.getByTestId("sign-in")).toHaveClass("is-success");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(500);
    });
    expect(screen.getByText("OPERATOR")).toBeInTheDocument();
  });
});
