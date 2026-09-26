/*
 * Deleting a fixture (spec §21.18, §9.7): group memberships are shown and
 * never block; a scene snapshot reference blocks the delete and lists the
 * scenes involved.
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { FixtureDeleteDialog } from "./FixtureDeleteDialog";

describe("FixtureDeleteDialog (§21.18, §9.7)", () => {
  it("shows group memberships without blocking the delete", () => {
    render(
      <FixtureDeleteDialog
        open
        onOpenChange={() => undefined}
        fixtureName="Stage Wash 6"
        groupNames={["Row 2", "Wash row"]}
        references={[]}
        loading={false}
        deleting={false}
        onConfirm={vi.fn()}
      />,
    );
    expect(screen.getByText("Row 2")).toBeInTheDocument();
    expect(screen.getByText("Wash row")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Delete fixture" })).not.toBeDisabled();
  });

  it("blocks the delete and lists the scenes when a snapshot references the fixture", () => {
    render(
      <FixtureDeleteDialog
        open
        onOpenChange={() => undefined}
        fixtureName="Stage Wash 6"
        groupNames={[]}
        references={[
          { entity: "scene", id: 1, name: "Act 1 opening" },
          { entity: "scene", id: 2, name: "Blackout" },
        ]}
        loading={false}
        deleting={false}
        onConfirm={vi.fn()}
      />,
    );
    expect(screen.getByText("Act 1 opening")).toBeInTheDocument();
    expect(screen.getByText("Blackout")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Delete fixture" })).toBeDisabled();
    expect(screen.getByText(/referenced by a saved look/)).toBeInTheDocument();
  });

  it("shows a loading state while references are still being fetched", () => {
    render(
      <FixtureDeleteDialog
        open
        onOpenChange={() => undefined}
        fixtureName="Stage Wash 6"
        groupNames={[]}
        references={[]}
        loading
        deleting={false}
        onConfirm={vi.fn()}
      />,
    );
    expect(screen.getByText("Checking…")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Delete fixture" })).toBeDisabled();
  });
});
