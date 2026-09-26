/*
 * The Certificates screen (spec §21.24 *Certificates*, §6.16, contracts §5–§6):
 * the token is never displayed, issuance drives `ProgressPanel` through its
 * six named steps, and a self-signed certificate gets the download-and-trust
 * notice.
 */
import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/api/client";
import { resetLiveState, setProgress } from "@/live/store";
import { renderWithProviders } from "@/test/render";

import { CertificatesScreen } from "./CertificatesScreen";
import type { CertificateCard, HistoryResponse } from "./types";

const client = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

const TRUSTED_CARD: CertificateCard = {
  domain: "av.school.nz",
  issuer: "Let's Encrypt",
  issued: "2026-03-12T00:00:00+13:00",
  expires: "2026-06-11T00:00:00+12:00",
  days_remaining: 60,
  self_signed: false,
  expired: false,
  renewal_history: [],
};

function serve(history: HistoryResponse, configured = false, overrides: Record<string, unknown> = {}) {
  client.api.mockImplementation((path: string, options?: { method?: string; body?: unknown }) => {
    const method = options?.method ?? (options?.body === undefined ? "GET" : "POST");
    if (path === "/system/certs/history") return Promise.resolve(history);
    if (path === "/system/certs/token" && method === "GET") return Promise.resolve({ configured });
    if (path in overrides) return overrides[path] as Promise<unknown>;
    return Promise.reject(new Error(`unexpected ${method} ${path}`));
  });
}

beforeEach(() => {
  client.api.mockReset();
  resetLiveState();
});

describe("CertificatesScreen — the token is write-only", () => {
  it("never renders the token's value, only whether one is set", async () => {
    serve({ certificate: TRUSTED_CARD, history: [] }, true);
    renderWithProviders(<CertificatesScreen />, { route: "/admin/certificates" });

    expect(await screen.findByText("av.school.nz")).toBeInTheDocument();
    // A masked placeholder, not a value the server never sent in the first place.
    const masked = screen.getByText("●●●●●●●●●●");
    expect(masked).toBeInTheDocument();
  });

  it("saves a new token without ever echoing it back, and clears the field", async () => {
    let saved: string | undefined;
    serve(
      { certificate: TRUSTED_CARD, history: [] },
      false,
      {
        "/system/certs/token": Promise.resolve({ configured: true }),
      },
    );
    client.api.mockImplementation((path: string, options?: { method?: string; body?: unknown }) => {
      const method = options?.method ?? (options?.body === undefined ? "GET" : "POST");
      if (path === "/system/certs/history") return Promise.resolve({ certificate: TRUSTED_CARD, history: [] });
      if (path === "/system/certs/token" && method === "GET") return Promise.resolve({ configured: saved !== undefined });
      if (path === "/system/certs/token" && method === "PUT") {
        saved = (options?.body as { token: string }).token;
        return Promise.resolve({ configured: true });
      }
      return Promise.reject(new Error(`unexpected ${method} ${path}`));
    });

    renderWithProviders(<CertificatesScreen />, { route: "/admin/certificates" });
    fireEvent.click(await screen.findByRole("button", { name: "Set" }));
    fireEvent.change(screen.getByLabelText("API token"), { target: { value: "cf-secret-abc123" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(saved).toBe("cf-secret-abc123"));
    expect(screen.queryByDisplayValue("cf-secret-abc123")).not.toBeInTheDocument();
    expect(await screen.findByText("●●●●●●●●●●")).toBeInTheDocument();
  });
});

describe("CertificatesScreen — issuance progress (§21.24: real steps, never a spinner)", () => {
  it("shows ProgressPanel with all six named steps while issuing, driven by live progress frames", async () => {
    serve({ certificate: TRUSTED_CARD, history: [] }, true, {
      "/system/certs/issue": new Promise(() => {
        /* left pending: the test drives progress frames itself */
      }),
    });
    renderWithProviders(<CertificatesScreen />, { route: "/admin/certificates" });

    fireEvent.click(await screen.findByRole("button", { name: "Renew now" }));

    expect(await screen.findByText("Requesting")).toBeInTheDocument();
    expect(screen.getByText("Reloading nginx")).toBeInTheDocument();

    act(() => setProgress({ operation: "cert_issue", step: 4, of: 6, message: "Verifying" }));
    const items = screen.getAllByRole("listitem");
    expect(items[3]).toHaveAttribute("data-state", "current");
  });
});

describe("CertificatesScreen — the self-signed notice (§6.16, §21.8)", () => {
  it("links to the download and states the trust steps when self-signed", async () => {
    const selfSigned: CertificateCard = { ...TRUSTED_CARD, issuer: "Proskenion (self-signed)", self_signed: true };
    serve({ certificate: selfSigned, history: [] }, false);
    renderWithProviders(<CertificatesScreen />, { route: "/admin/certificates" });

    expect(await screen.findByText("Using a self-signed certificate")).toBeInTheDocument();
    const link = screen.getByRole("link", { name: "Download the certificate" });
    expect(link).toHaveAttribute("href", "/api/v1/system/certs/download");
    expect(screen.getByText(/VPN & Device Management/)).toBeInTheDocument();
  });
});

describe('CertificatesScreen — "Use self-signed" (§21.24, §6.16, contracts §5 wave 3)', () => {
  it("explains what it costs before doing it, then calls the endpoint that needs neither Cloudflare nor ACME", async () => {
    let selfSignedCalled = false;
    client.api.mockImplementation((path: string, options?: { method?: string; body?: unknown }) => {
      const method = options?.method ?? (options?.body === undefined ? "GET" : "POST");
      if (path === "/system/certs/history") return Promise.resolve({ certificate: TRUSTED_CARD, history: [] });
      if (path === "/system/certs/token" && method === "GET") return Promise.resolve({ configured: true });
      if (path === "/system/certs/self-signed") {
        selfSignedCalled = true;
        return Promise.resolve({ certificate: { ...TRUSTED_CARD, self_signed: true, issuer: "Proskenion (self-signed)" } });
      }
      return Promise.reject(new Error(`unexpected ${method} ${path}`));
    });
    renderWithProviders(<CertificatesScreen />, { route: "/admin/certificates" });

    fireEvent.click(await screen.findByRole("button", { name: "Use self-signed" }));

    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent(/Every browser will warn/);
    expect(dialog).toHaveTextContent(/an iPad will refuse the live connection/);
    // Not called yet — only the explanation has appeared so far.
    expect(selfSignedCalled).toBe(false);

    fireEvent.click(within(dialog).getByRole("button", { name: "Use self-signed" }));

    await waitFor(() =>
      expect(client.api).toHaveBeenCalledWith("/system/certs/self-signed", expect.objectContaining({ body: { hostname: null } })),
    );
    expect(selfSignedCalled).toBe(true);
  });

  it("reports a failure inline rather than silently", async () => {
    client.api.mockImplementation((path: string, options?: { method?: string; body?: unknown }) => {
      const method = options?.method ?? (options?.body === undefined ? "GET" : "POST");
      if (path === "/system/certs/history") return Promise.resolve({ certificate: TRUSTED_CARD, history: [] });
      if (path === "/system/certs/token" && method === "GET") return Promise.resolve({ configured: true });
      if (path === "/system/certs/self-signed") {
        return Promise.reject(new ApiError(422, "validation_failed", "no hostname is configured for this certificate"));
      }
      return Promise.reject(new Error(`unexpected ${method} ${path}`));
    });
    renderWithProviders(<CertificatesScreen />, { route: "/admin/certificates" });

    fireEvent.click(await screen.findByRole("button", { name: "Use self-signed" }));
    const dialog = await screen.findByRole("alertdialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Use self-signed" }));

    expect(await screen.findByText("no hostname is configured for this certificate")).toBeInTheDocument();
  });
});

describe("CertificatesScreen — renewal history", () => {
  it("renders each attempt's date, method and result", async () => {
    serve(
      {
        certificate: TRUSTED_CARD,
        history: [
          { attempted_at: "2026-06-11T03:00:00+12:00", method: "automatic", result: "success", issuer: "Let's Encrypt", serial: "1", detail: null },
          { attempted_at: "2026-05-11T03:00:00+12:00", method: "automatic", result: "failed", issuer: null, serial: null, detail: "DNS timeout" },
        ],
      },
      true,
    );
    renderWithProviders(<CertificatesScreen />, { route: "/admin/certificates" });

    expect(await screen.findByText("Success")).toBeInTheDocument();
    expect(screen.getByText("Failed")).toBeInTheDocument();
    expect(screen.getByText("DNS timeout")).toBeInTheDocument();
  });

  it("shows an empty state with nothing recorded yet", async () => {
    serve({ certificate: null, history: [] }, false);
    renderWithProviders(<CertificatesScreen />, { route: "/admin/certificates" });
    expect(await screen.findByText("No renewal attempts recorded yet.")).toBeInTheDocument();
    expect(screen.getByText("No certificate installed")).toBeInTheDocument();
  });
});
