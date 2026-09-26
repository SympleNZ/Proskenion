/* Test write (§21.19 *Test write*): confirms first, disabled for incoming-only. */
import { fireEvent, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "@/test/render";

import { ADDRESS, OUTGOING_ADDRESS } from "./fixtures";
import { TestWriteForm } from "./TestWriteForm";

const client = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

describe("TestWriteForm", () => {
  beforeEach(() => {
    client.api.mockReset();
  });

  it("is disabled, with the reason, on an incoming-only address", () => {
    renderWithProviders(<TestWriteForm address={ADDRESS} />);
    expect(screen.getByRole("button", { name: "Test write" })).toBeDisabled();
    expect(screen.getByText(/Incoming-only/)).toBeInTheDocument();
  });

  it("asks for confirmation, naming the address, before sending — and only sends after confirming", async () => {
    client.api.mockResolvedValue({ ok: true });
    renderWithProviders(<TestWriteForm address={OUTGOING_ADDRESS} />);

    fireEvent.click(screen.getByRole("button", { name: "Test write" }));
    expect(client.api).not.toHaveBeenCalled();

    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent(OUTGOING_ADDRESS.name);
    expect(dialog).toHaveTextContent(OUTGOING_ADDRESS.group_address);

    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    expect(await screen.findByText(/Sent to/)).toBeInTheDocument();
    expect(client.api).toHaveBeenCalledWith(
      `/knx/addresses/${OUTGOING_ADDRESS.id}/test-write`,
      expect.objectContaining({ method: "POST" }),
    );
  });
});
