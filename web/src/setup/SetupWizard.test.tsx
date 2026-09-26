/*
 * First-run wizard (spec §10.4, §6.16, §16.4).
 *
 * The wizard is the only bootstrap path, so what is tested is what an
 * installer relies on: an abandoned setup resumes at the first incomplete
 * step with summaries for what is done, a refused password shows the server's
 * own per-field message, and step 6 shows the unavailable option with its
 * reason rather than hiding it.
 */
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { CERTIFICATE_CHANGED_MESSAGE, CERTIFICATE_REFUSED_MESSAGE, clearCertificateChange } from "@/api/certificateChange";
import { ApiError, NetworkError } from "@/api/client";
import { renderWithProviders } from "@/test/render";

import { SetupWizard } from "./SetupWizard";
import type { SetupState, SetupStep, StepKey } from "./types";

const client = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

const LABELS: Record<number, [StepKey, string]> = {
  1: ["welcome", "Welcome"],
  2: ["admin_password", "Admin password"],
  3: ["network", "Network"],
  4: ["devices", "Devices"],
  5: ["operator_password", "Operator password"],
  6: ["certificate", "Certificate"],
  7: ["summary", "Summary and commit"],
};

function steps(completed: Record<number, Record<string, unknown>>): SetupStep[] {
  return [1, 2, 3, 4, 5, 6, 7].map((n) => {
    const [key, label] = LABELS[n]!;
    const summary = completed[n];
    return {
      step: n,
      key,
      label,
      completed: summary !== undefined,
      completed_at: summary === undefined ? null : "2026-09-10T19:00:00+12:00",
      summary: summary ?? {},
    };
  });
}

const LETS_ENCRYPT_REASON =
  "Let's Encrypt via Cloudflare DNS-01 arrives in Phase 6. Generate a self-signed certificate now; it can be replaced without repeating the wizard.";

function state(completed: Record<number, Record<string, unknown>>): SetupState {
  const list = steps(completed);
  return {
    first_run: true,
    steps: list,
    next_step: list.find((step) => !step.completed)?.step ?? null,
    detected: {
      locale: "en_NZ.UTF-8",
      timezone: "Pacific/Auckland",
      platform: "Raspberry Pi CM5",
      hostname: "auditorium.school.nz",
      address: "10.2.30.40",
    },
    certificate: {
      hostname: "auditorium.school.nz",
      options: [
        {
          id: "self_signed",
          label: "Generate a self-signed certificate",
          available: true,
          reason: null,
          guidance: [
            "Download the certificate from this controller on the iPad.",
            "Open Settings and install the downloaded profile.",
            "Settings → General → VPN & Device Management → trust the certificate.",
          ],
        },
        {
          id: "lets_encrypt",
          label: "Issue via Let's Encrypt (Cloudflare DNS-01)",
          available: false,
          reason: LETS_ENCRYPT_REASON,
          guidance: [],
        },
      ],
      installed: null,
    },
  };
}

function serve(setupState: SetupState) {
  client.api.mockImplementation((path: string) => {
    if (path === "/setup/state") return Promise.resolve(setupState);
    return Promise.resolve({});
  });
}

describe("SetupWizard", () => {
  beforeEach(() => {
    client.api.mockReset();
    clearCertificateChange();
  });

  it("opens at the first incomplete step and summarises the ones already done", async () => {
    serve(
      state({
        1: { locale: "en_NZ.UTF-8", timezone: "Pacific/Auckland" },
        2: { admin_password_set: true, operator_seeded: true },
      }),
    );
    renderWithProviders(<SetupWizard />, { route: "/setup" });

    expect(await screen.findByText(/Step 3 of 7 — Network/)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Network", level: 2 })).toBeInTheDocument();

    const progress = screen.getByRole("navigation", { name: "Setup progress" });
    expect(within(progress).getByText("en_NZ.UTF-8 · Pacific/Auckland")).toBeInTheDocument();
    expect(
      within(progress).getByText("Admin password set; the operator account was seeded and is replaced at step 5"),
    ).toBeInTheDocument();
    // A completed step can be revisited; an incomplete one has nothing to edit.
    expect(within(progress).getByRole("button", { name: "Edit Welcome" })).toBeInTheDocument();
    expect(within(progress).queryByRole("button", { name: "Edit Devices" })).not.toBeInTheDocument();
  });

  it("resumes at the review when every step has been recorded", async () => {
    serve(
      state({
        1: { locale: "en_NZ.UTF-8" },
        2: { admin_password_set: true },
        3: { skipped: true },
        4: { device_count: 2 },
        5: { operator_password_set: true },
        6: { option: "self_signed", hostname: "auditorium.school.nz" },
      }),
    );
    renderWithProviders(<SetupWizard />, { route: "/setup" });

    expect(await screen.findByText(/Step 7 of 7/)).toBeInTheDocument();
    const review = screen.getByRole("heading", { name: "Summary and commit", level: 2 }).parentElement as HTMLElement;
    expect(within(review).getByText("Skipped — the network was already correct")).toBeInTheDocument();
    expect(within(review).getByText("2 devices configured")).toBeInTheDocument();
    expect(within(review).getByText(/Self-signed certificate · auditorium.school.nz/)).toBeInTheDocument();
  });

  it("shows the server's per-field message when a password is too short", async () => {
    serve(state({ 1: { locale: "en_NZ.UTF-8" } }));
    client.api.mockImplementation((path: string) => {
      if (path === "/setup/state") return Promise.resolve(state({ 1: { locale: "en_NZ.UTF-8" } }));
      if (path === "/setup/step/2") {
        return Promise.reject(
          new ApiError(422, "validation_failed", "The admin password was not accepted.", {
            fields: [{ field: "password", message: "The password must be at least 12 characters.", type: "too_short" }],
          }),
        );
      }
      return Promise.resolve({});
    });
    renderWithProviders(<SetupWizard />, { route: "/setup" });

    const panel = await screen.findByRole("region", { name: "Admin password" });
    fireEvent.change(within(panel).getByLabelText("Admin password"), { target: { value: "abcdefghijkl" } });
    fireEvent.change(within(panel).getByLabelText("Enter it again"), { target: { value: "abcdefghijkl" } });
    fireEvent.click(within(panel).getByRole("button", { name: "Set the password" }));

    expect(await screen.findByText("The password must be at least 12 characters.")).toBeInTheDocument();
    expect(screen.getByText("That was not accepted")).toBeInTheDocument();
    // Forms never clear on error — what was typed is preserved (§21.27).
    expect(within(panel).getByLabelText("Admin password")).toHaveValue("abcdefghijkl");
  });

  it("refuses a short password and a mismatch before the request is made", async () => {
    serve(state({ 1: { locale: "en_NZ.UTF-8" } }));
    renderWithProviders(<SetupWizard />, { route: "/setup" });

    const panel = await screen.findByRole("region", { name: "Admin password" });
    fireEvent.change(within(panel).getByLabelText("Admin password"), { target: { value: "short" } });
    fireEvent.change(within(panel).getByLabelText("Enter it again"), { target: { value: "different" } });
    fireEvent.click(within(panel).getByRole("button", { name: "Set the password" }));

    expect(await screen.findByText("The password must be at least 12 characters.")).toBeInTheDocument();
    expect(screen.getByText("The two passwords do not match.")).toBeInTheDocument();
    expect(client.api).not.toHaveBeenCalledWith("/setup/step/2", expect.anything());
  });

  it("offers both certificate options with Let's Encrypt disabled and the reason shown", async () => {
    serve(
      state({
        1: { locale: "en_NZ.UTF-8" },
        2: { admin_password_set: true },
        3: { skipped: true },
        4: { skipped: true },
        5: { operator_password_set: true },
      }),
    );
    renderWithProviders(<SetupWizard />, { route: "/setup" });

    const letsEncrypt = await screen.findByRole("radio", { name: "Issue via Let's Encrypt (Cloudflare DNS-01)" });
    expect(letsEncrypt).toBeDisabled();
    expect(screen.getByText(LETS_ENCRYPT_REASON)).toBeInTheDocument();
    expect(letsEncrypt).toHaveAttribute("aria-describedby", "cert-lets_encrypt-reason");

    const selfSigned = screen.getByRole("radio", { name: "Generate a self-signed certificate" });
    expect(selfSigned).toBeEnabled();
    expect(selfSigned).toBeChecked();
    // §6.16: the self-signed path carries the iOS trust guidance.
    expect(screen.getByText(/VPN & Device Management/)).toBeInTheDocument();
  });

  it("moves to the step the server names next, so nothing submitted is repeated", async () => {
    const first = state({ 1: { locale: "en_NZ.UTF-8" } });
    const after = state({ 1: { locale: "en_NZ.UTF-8" }, 2: { admin_password_set: true } });
    client.api.mockImplementation((path: string) => {
      if (path === "/setup/state") return Promise.resolve(first);
      if (path === "/setup/step/2") {
        return Promise.resolve({ step: after.steps[1], next_step: 3, steps: after.steps });
      }
      return Promise.resolve({});
    });
    renderWithProviders(<SetupWizard />, { route: "/setup" });

    const panel = await screen.findByRole("region", { name: "Admin password" });
    fireEvent.change(within(panel).getByLabelText("Admin password"), { target: { value: "twelve-characters" } });
    fireEvent.change(within(panel).getByLabelText("Enter it again"), { target: { value: "twelve-characters" } });
    fireEvent.click(within(panel).getByRole("button", { name: "Set the password" }));

    await waitFor(() => expect(screen.getByText(/Step 3 of 7 — Network/)).toBeInTheDocument());
  });

  it("renders the devices interface for step 4 and Skip still advances the wizard", async () => {
    const at4 = state({
      1: { locale: "en_NZ.UTF-8" },
      2: { admin_password_set: true },
      3: { skipped: true },
    });
    const after = state({
      1: { locale: "en_NZ.UTF-8" },
      2: { admin_password_set: true },
      3: { skipped: true },
      4: { skipped: true, device_count: 0 },
    });
    client.api.mockImplementation((path: string) => {
      if (path === "/setup/state") return Promise.resolve(at4);
      if (path === "/drivers") return Promise.resolve({ drivers: [] });
      if (path === "/devices") return Promise.resolve({ devices: [] });
      if (path === "/setup/step/4") {
        return Promise.resolve({ step: after.steps[3], next_step: 5, steps: after.steps });
      }
      return Promise.resolve({});
    });
    renderWithProviders(<SetupWizard />, { route: "/setup" });

    expect(await screen.findByText(/Step 4 of 7 — Devices/)).toBeInTheDocument();
    // The devices screen itself renders — no trace of the old gate workaround.
    expect(await screen.findByText("No devices configured")).toBeInTheDocument();
    expect(screen.queryByText(/first-run setup commits/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add a device" })).toBeEnabled();

    fireEvent.click(screen.getByRole("button", { name: "Skip — configure devices later" }));

    await waitFor(() => expect(screen.getByText(/Step 5 of 7 — Operator password/)).toBeInTheDocument());
  });

  describe("a certificate step that replaced the certificate (the rebuilt appliance, 25 Sep 2026)", () => {
    const THROUGH_FIVE = {
      1: { locale: "en_NZ.UTF-8" },
      2: { admin_password_set: true },
      3: { skipped: true },
      4: { skipped: true },
      5: { operator_password_set: true },
    };
    const AFTER_SIX = { ...THROUGH_FIVE, 6: { option: "self_signed", hostname: "auditorium.school.nz", self_signed: true } };

    function serveStepSix(replaced: boolean) {
      const before = state(THROUGH_FIVE);
      const after = state(AFTER_SIX);
      client.api.mockImplementation((path: string) => {
        if (path === "/setup/state") return Promise.resolve(before);
        if (path === "/setup/step/6") {
          // The server's own step-6 response shape (proskenion/api/setup.py).
          return Promise.resolve({
            step: after.steps[5],
            next_step: 7,
            steps: after.steps,
            certificate_replaced: replaced,
            certificate_names: ["auditorium.school.nz", "localhost", "10.2.30.40"],
          });
        }
        // The browser refused the new certificate: fetch rejects, and the
        // client turns that into its NetworkError.
        if (path === "/setup/step/7") return Promise.reject(new NetworkError());
        return Promise.resolve({});
      });
    }

    async function generateThenReview() {
      renderWithProviders(<SetupWizard />, { route: "/setup" });
      fireEvent.click(await screen.findByRole("button", { name: "Generate the certificate" }));
      await waitFor(() => expect(screen.getByText(/Step 7 of 7/)).toBeInTheDocument());
    }

    it("warns before the next action, with a Reload button", async () => {
      serveStepSix(true);
      await generateThenReview();

      expect(screen.getByText("The controller's certificate has changed")).toBeInTheDocument();
      expect(screen.getByText(CERTIFICATE_CHANGED_MESSAGE)).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Reload" })).toBeInTheDocument();
    });

    it("explains a network failure on the next step as the certificate, not an unreachable controller", async () => {
      serveStepSix(true);
      await generateThenReview();

      fireEvent.click(screen.getByRole("button", { name: "Mark reviewed" }));
      expect(await screen.findByText(CERTIFICATE_REFUSED_MESSAGE)).toBeInTheDocument();
      expect(screen.queryByText("Could not reach the controller")).not.toBeInTheDocument();
    });

    it("says nothing about the certificate when step 6 kept the one the browser accepted", async () => {
      serveStepSix(false);
      await generateThenReview();

      expect(screen.queryByText("The controller's certificate has changed")).not.toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "Mark reviewed" }));
      expect(await screen.findByText("Could not reach the controller")).toBeInTheDocument();
    });
  });

  it("says the wizard is closed once setup has already committed", async () => {
    client.api.mockRejectedValue(
      new ApiError(403, "permission_denied", "First-run setup is already complete.", { reason: "setup_complete" }),
    );
    renderWithProviders(<SetupWizard />, { route: "/setup" });

    expect(await screen.findByText("This controller is already set up")).toBeInTheDocument();
  });
});
