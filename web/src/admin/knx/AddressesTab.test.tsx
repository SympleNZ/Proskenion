/* Delete protection on the Addresses tab (§21.19 *ReferenceGuard*, §16.1 `in_use`). */
import { fireEvent, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/api/client";
import { renderWithProviders } from "@/test/render";

import { AddressesTab } from "./AddressesTab";
import { ADDRESS } from "./fixtures";

const client = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

describe("AddressesTab delete protection", () => {
  beforeEach(() => {
    client.api.mockReset();
  });

  it("shows the references — grouped by what refers to it, linking where a screen exists — when a delete is refused with 409 in_use", async () => {
    client.api.mockImplementation((path: string, options?: { method?: string }) => {
      // `entity` values here mirror `proskenion/db/crud/knx.py`'s
      // `_references_address` exactly — "rules" and "scene_actions", never
      // the generic "scene" a first guess might reach for.
      if (path === `/knx/addresses/${ADDRESS.id}` && options?.method === "DELETE") {
        return Promise.reject(
          new ApiError(409, "in_use", "This address is referenced elsewhere and cannot be removed", {
            references: [
              { entity: "rules", id: 5, name: "Performance Start" },
              { entity: "scene_actions", id: 9, name: "All Off" },
            ],
          }),
        );
      }
      return Promise.resolve({});
    });

    renderWithProviders(<AddressesTab addresses={[ADDRESS]} deviceGroups={[]} />);

    fireEvent.click(await screen.findByRole("button", { name: "Delete" }));
    const confirm = await screen.findByRole("alertdialog", { name: `Delete "${ADDRESS.name}"?` });
    fireEvent.click(within(confirm).getByRole("button", { name: "Delete" }));

    const guard = await screen.findByRole("alertdialog", { name: `Cannot delete "${ADDRESS.name}"` });
    expect(within(guard).getByText("Performance Start")).toBeInTheDocument();
    expect(within(guard).getByText("All Off")).toBeInTheDocument();
    // A rule links to the Rules screen; a scene action's reference carries the
    // action's id, not its scene's, so it stays a plain name.
    expect(within(guard).getByRole("link", { name: "Performance Start" })).toHaveAttribute("href", "/admin/rules");
    expect(within(guard).queryByRole("link", { name: "All Off" })).not.toBeInTheDocument();
  });
});
