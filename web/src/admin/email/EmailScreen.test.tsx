/*
 * The Email screen (spec §21.24 *Email*, contracts §5): the password is
 * write-only, the test button proves an unsaved edit and reports inline
 * what stage failed, and saving mirrors to the emergency fallback (stated,
 * not proved here — that is `smtp-fallback.toml`'s own backend test).
 */
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/api/client";
import { renderWithProviders } from "@/test/render";

import { EmailScreen } from "./EmailScreen";
import type { EmailConfig } from "./types";

const client = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

const CONFIG: EmailConfig = {
  host: "relay.n4l.co.nz",
  port: 25,
  tls_mode: "starttls",
  username: null,
  password_set: false,
  sender: "auditorium@school.nz",
  recipient: "ict@obhs.school.nz",
  updated_at: "2026-09-01T00:00:00+12:00",
};

function serve(config: EmailConfig | null, overrides: Record<string, unknown> = {}) {
  client.api.mockImplementation((path: string, options?: { method?: string; body?: unknown }) => {
    const method = options?.method ?? (options?.body === undefined ? "GET" : "POST");
    if (path === "/system/email" && method === "GET") {
      return config
        ? Promise.resolve(config)
        : Promise.resolve({
            host: null,
            port: null,
            tls_mode: "starttls",
            username: null,
            password_set: false,
            sender: null,
            recipient: null,
            updated_at: null,
          });
    }
    const key = `${method} ${path}`;
    if (key in overrides) return overrides[key] as Promise<unknown>;
    return Promise.reject(new Error(`unexpected ${method} ${path}`));
  });
}

beforeEach(() => {
  client.api.mockReset();
});

describe("EmailScreen — the password is write-only", () => {
  it("never pre-fills the password field, even when one is set", async () => {
    serve({ ...CONFIG, password_set: true });
    renderWithProviders(<EmailScreen />, { route: "/admin/email" });
    expect(await screen.findByDisplayValue("relay.n4l.co.nz")).toBeInTheDocument();
    expect(screen.getByLabelText("Password (set — leave blank to keep it)")).toHaveValue("");
    expect(screen.getByLabelText("Password (set — leave blank to keep it)")).toHaveAttribute("type", "password");
  });

  it("saves without a password field when left blank — the stored one is kept, not cleared", async () => {
    let sentBody: unknown;
    serve({ ...CONFIG, password_set: true }, {
      "PUT /system/email": new Promise((resolve) => {
        resolve({ ...CONFIG, password_set: true });
      }),
    });
    client.api.mockImplementation((path: string, options?: { method?: string; body?: unknown }) => {
      const method = options?.method ?? (options?.body === undefined ? "GET" : "POST");
      if (path === "/system/email" && method === "GET") return Promise.resolve({ ...CONFIG, password_set: true });
      if (path === "/system/email" && method === "PUT") {
        sentBody = options?.body;
        return Promise.resolve({ ...CONFIG, password_set: true });
      }
      return Promise.reject(new Error(`unexpected ${method} ${path}`));
    });

    renderWithProviders(<EmailScreen />, { route: "/admin/email" });
    await screen.findByDisplayValue("relay.n4l.co.nz");
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(sentBody).toBeDefined());
    expect(sentBody).not.toHaveProperty("password");
  });
});

describe("EmailScreen — validation (defaults suit an unauthenticated relay on 25, Q22)", () => {
  it("defaults to port 25 and STARTTLS with nothing saved yet", async () => {
    serve(null);
    renderWithProviders(<EmailScreen />, { route: "/admin/email" });
    expect(await screen.findByDisplayValue("25")).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "TLS mode" })).toHaveValue("starttls");
  });

  it("reports missing host, port, sender and recipient on Save without submitting", async () => {
    serve(null);
    renderWithProviders(<EmailScreen />, { route: "/admin/email" });
    await screen.findByDisplayValue("25");
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByText("Enter the SMTP host")).toBeInTheDocument();
    expect(screen.getByText("Enter a sender address")).toBeInTheDocument();
    expect(screen.getByText("Enter a recipient address")).toBeInTheDocument();
    expect(client.api.mock.calls.every(([, options]) => (options as { method?: string } | undefined)?.method !== "PUT")).toBe(
      true,
    );
  });
});

describe("EmailScreen — the test button reports inline what failed", () => {
  it.each([
    ["dns", "Could not resolve the host"],
    ["connect", "Could not connect"],
    ["tls", "TLS could not be established"],
    ["auth", "Authentication failed"],
    ["rejected_recipient", "The recipient was rejected"],
  ] as const)("shows the %s stage's plain-word label", async (stage, label) => {
    serve(CONFIG, {
      "POST /system/email/test": Promise.resolve({ ok: false, stage, message: "the server's own detail" }),
    });
    renderWithProviders(<EmailScreen />, { route: "/admin/email" });
    await screen.findByDisplayValue("relay.n4l.co.nz");
    fireEvent.click(screen.getByRole("button", { name: "Send a test email" }));

    expect(await screen.findByText(`${label} — the server's own detail`)).toBeInTheDocument();
  });

  it("reports success plainly", async () => {
    serve(CONFIG, { "POST /system/email/test": Promise.resolve({ ok: true, stage: null, message: "Test email sent" }) });
    renderWithProviders(<EmailScreen />, { route: "/admin/email" });
    await screen.findByDisplayValue("relay.n4l.co.nz");
    fireEvent.click(screen.getByRole("button", { name: "Send a test email" }));

    expect(await screen.findByText("Test email sent")).toBeInTheDocument();
  });

  it("tests the form's current values, unsaved, so a draft edit can be proved first", async () => {
    let sentBody: unknown;
    serve(CONFIG);
    client.api.mockImplementation((path: string, options?: { method?: string; body?: unknown }) => {
      const method = options?.method ?? (options?.body === undefined ? "GET" : "POST");
      if (path === "/system/email" && method === "GET") return Promise.resolve(CONFIG);
      if (path === "/system/email/test") {
        sentBody = options?.body;
        return Promise.resolve({ ok: true, stage: null, message: "Test email sent" });
      }
      return Promise.reject(new Error(`unexpected ${method} ${path}`));
    });

    renderWithProviders(<EmailScreen />, { route: "/admin/email" });
    await screen.findByDisplayValue("relay.n4l.co.nz");
    fireEvent.change(screen.getByLabelText("Host"), { target: { value: "relay.example.test" } });
    fireEvent.click(screen.getByRole("button", { name: "Send a test email" }));

    await waitFor(() => expect(sentBody).toMatchObject({ host: "relay.example.test" }));
  });
});

describe("EmailScreen — removing the mail settings (DELETE /system/email)", () => {
  it("offers no remove button when nothing is configured", async () => {
    serve(null);
    renderWithProviders(<EmailScreen />, { route: "/admin/email" });
    await screen.findByDisplayValue("25");
    expect(screen.queryByRole("button", { name: "Remove mail settings" })).not.toBeInTheDocument();
  });

  it("asks for confirmation before calling DELETE", async () => {
    let deleteCalled = false;
    serve(CONFIG);
    client.api.mockImplementation((path: string, options?: { method?: string; body?: unknown }) => {
      const method = options?.method ?? (options?.body === undefined ? "GET" : "POST");
      if (path === "/system/email" && method === "GET") return Promise.resolve(CONFIG);
      if (path === "/system/email" && method === "DELETE") {
        deleteCalled = true;
        return Promise.resolve(undefined);
      }
      return Promise.reject(new Error(`unexpected ${method} ${path}`));
    });
    renderWithProviders(<EmailScreen />, { route: "/admin/email" });
    await screen.findByDisplayValue("relay.n4l.co.nz");

    fireEvent.click(screen.getByRole("button", { name: "Remove mail settings" }));

    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent(/Clears the SMTP relay/);
    // Not called yet — only the confirmation has appeared so far.
    expect(deleteCalled).toBe(false);

    fireEvent.click(within(dialog).getByRole("button", { name: "Remove mail settings" }));

    await waitFor(() => expect(deleteCalled).toBe(true));
  });

  it("refreshes the screen after removing", async () => {
    let getCalls = 0;
    serve(CONFIG);
    client.api.mockImplementation((path: string, options?: { method?: string; body?: unknown }) => {
      const method = options?.method ?? (options?.body === undefined ? "GET" : "POST");
      if (path === "/system/email" && method === "GET") {
        getCalls += 1;
        return Promise.resolve(
          getCalls === 1
            ? CONFIG
            : { host: null, port: null, tls_mode: "starttls", username: null, password_set: false, sender: null, recipient: null, updated_at: null },
        );
      }
      if (path === "/system/email" && method === "DELETE") return Promise.resolve(undefined);
      return Promise.reject(new Error(`unexpected ${method} ${path}`));
    });
    renderWithProviders(<EmailScreen />, { route: "/admin/email" });
    await screen.findByDisplayValue("relay.n4l.co.nz");

    fireEvent.click(screen.getByRole("button", { name: "Remove mail settings" }));
    const dialog = await screen.findByRole("alertdialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Remove mail settings" }));

    await screen.findByText("No email configured");
    expect(screen.queryByDisplayValue("relay.n4l.co.nz")).not.toBeInTheDocument();
  });

  it("reports a failure inline rather than silently", async () => {
    serve(CONFIG);
    client.api.mockImplementation((path: string, options?: { method?: string; body?: unknown }) => {
      const method = options?.method ?? (options?.body === undefined ? "GET" : "POST");
      if (path === "/system/email" && method === "GET") return Promise.resolve(CONFIG);
      if (path === "/system/email" && method === "DELETE") {
        return Promise.reject(new ApiError(500, "internal_error", "could not update the firewall"));
      }
      return Promise.reject(new Error(`unexpected ${method} ${path}`));
    });
    renderWithProviders(<EmailScreen />, { route: "/admin/email" });
    await screen.findByDisplayValue("relay.n4l.co.nz");

    fireEvent.click(screen.getByRole("button", { name: "Remove mail settings" }));
    const dialog = await screen.findByRole("alertdialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Remove mail settings" }));

    expect(await screen.findByText("could not update the firewall")).toBeInTheDocument();
  });
});
