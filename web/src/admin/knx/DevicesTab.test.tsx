/* Device group delete protection (§21.19 *Devices tab*, §16.1 `in_use`). */
import { fireEvent, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/api/client";
import { renderWithProviders } from "@/test/render";

import { DevicesTab } from "./DevicesTab";
import { ADDRESS, DEVICE_GROUP } from "./fixtures";

const client = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

describe("DevicesTab delete protection", () => {
  beforeEach(() => {
    client.api.mockReset();
  });

  it("shows the assigned addresses when a delete is refused with 409 in_use", async () => {
    client.api.mockImplementation((path: string, options?: { method?: string }) => {
      if (path === `/knx/device-groups/${DEVICE_GROUP.id}` && options?.method === "DELETE") {
        // "knx_group_addresses" is `ADDRESSES_TABLE` in
        // `proskenion/db/crud/knx.py` — the real entity name
        // `_references_device_group` returns.
        return Promise.reject(
          new ApiError(409, "in_use", "This device group has addresses assigned and cannot be removed", {
            references: [{ entity: "knx_group_addresses", id: ADDRESS.id, name: ADDRESS.name }],
          }),
        );
      }
      return Promise.resolve({});
    });

    renderWithProviders(<DevicesTab deviceGroups={[DEVICE_GROUP]} addresses={[ADDRESS]} />);

    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    const confirm = await screen.findByRole("alertdialog", { name: `Delete "${DEVICE_GROUP.name}"?` });
    fireEvent.click(within(confirm).getByRole("button", { name: "Delete" }));

    expect(await screen.findByText(`Cannot delete "${DEVICE_GROUP.name}"`)).toBeInTheDocument();
    expect(await screen.findByText(ADDRESS.name)).toBeInTheDocument();
  });

  it("expands a group to show its addresses", () => {
    renderWithProviders(<DevicesTab deviceGroups={[DEVICE_GROUP]} addresses={[ADDRESS]} />);
    fireEvent.click(screen.getByRole("button", { name: `Expand ${DEVICE_GROUP.name}` }));
    expect(screen.getByText(ADDRESS.group_address)).toBeInTheDocument();
  });
});
