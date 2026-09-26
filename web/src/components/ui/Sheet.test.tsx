/*
 * §24.3 and §24.7: "Every modal and sheet traps focus and returns it on
 * close" and "Escape closes every modal and sheet". Every admin sheet and
 * confirmation is built on these two primitives (or directly on Radix Dialog,
 * the same library), so what holds here holds for each of them.
 *
 * Radix's own modal Dialog returns focus on close only to its own
 * `Dialog.Trigger`, and the interface opens almost every sheet and every
 * ConfirmDialog through a controlled `open` prop from an ordinary button, with
 * no Trigger — `useReturnFocus` in `./Sheet.tsx` fixes this centrally, and
 * the two cases below (previously `it.fails`, recorded as a known defect in
 * P7-T13, docs/phase-7-milestone.md) now hold.
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it } from "vitest";

import { Button } from "./Button";
import { ConfirmDialog, Sheet, SheetContent, SheetTrigger } from "./Sheet";

function TriggeredSheet() {
  return (
    <>
      <button type="button">Before</button>
      <Sheet>
        <SheetTrigger asChild>
          <Button>Open the sheet</Button>
        </SheetTrigger>
        <SheetContent title="Edit fixture">
          <input aria-label="Name" />
          <input aria-label="Address" />
        </SheetContent>
      </Sheet>
      <button type="button">After</button>
    </>
  );
}

/** The way the admin screens open a sheet: state set by an ordinary button. */
function ControlledSheet() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <Button onClick={() => setOpen(true)}>Open the sheet</Button>
      <Sheet open={open} onOpenChange={setOpen}>
        <SheetContent title="Edit fixture">
          <input aria-label="Name" />
          <input aria-label="Address" />
        </SheetContent>
      </Sheet>
    </>
  );
}

function ControlledConfirm() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <Button onClick={() => setOpen(true)}>Delete scene</Button>
      <ConfirmDialog
        open={open}
        onOpenChange={setOpen}
        title="Delete Assembly?"
        description="The scene and its actions are removed. A snapshot is taken first."
        confirmLabel="Delete"
        destructive
        onConfirm={() => setOpen(false)}
      />
    </>
  );
}

async function expectTabStaysInside(user: ReturnType<typeof userEvent.setup>, dialog: HTMLElement): Promise<void> {
  for (let i = 0; i < 8; i++) {
    await user.tab();
    expect(dialog).toContainElement(document.activeElement as HTMLElement);
  }
}

describe("SheetContent (§24.3, §24.7)", () => {
  it("keeps focus inside while open: Tab never reaches the page behind", async () => {
    const user = userEvent.setup();
    render(<ControlledSheet />);
    await user.click(screen.getByRole("button", { name: "Open the sheet" }));
    await expectTabStaysInside(user, await screen.findByRole("dialog", { name: "Edit fixture" }));
  });

  it("closes on Escape", async () => {
    const user = userEvent.setup();
    render(<ControlledSheet />);
    await user.click(screen.getByRole("button", { name: "Open the sheet" }));
    await screen.findByRole("dialog", { name: "Edit fixture" });
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("returns focus to its SheetTrigger on close", async () => {
    const user = userEvent.setup();
    render(<TriggeredSheet />);
    const trigger = screen.getByRole("button", { name: "Open the sheet" });
    await user.click(trigger);
    await screen.findByRole("dialog", { name: "Edit fixture" });
    await user.keyboard("{Escape}");
    expect(trigger).toHaveFocus();
  });

  it("returns focus to the button that opened a controlled sheet", async () => {
    const user = userEvent.setup();
    render(<ControlledSheet />);
    const opener = screen.getByRole("button", { name: "Open the sheet" });
    await user.click(opener);
    await screen.findByRole("dialog", { name: "Edit fixture" });
    await user.keyboard("{Escape}");
    expect(opener).toHaveFocus();
  });
});

describe("ConfirmDialog (§24.3, §24.7)", () => {
  it("keeps focus inside while open, and closes on Escape", async () => {
    const user = userEvent.setup();
    render(<ControlledConfirm />);
    await user.click(screen.getByRole("button", { name: "Delete scene" }));
    const dialog = await screen.findByRole("alertdialog", { name: "Delete Assembly?" });
    await expectTabStaysInside(user, dialog);
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  });

  it("returns focus to the button that opened it", async () => {
    const user = userEvent.setup();
    render(<ControlledConfirm />);
    const opener = screen.getByRole("button", { name: "Delete scene" });
    await user.click(opener);
    await screen.findByRole("alertdialog", { name: "Delete Assembly?" });
    await user.keyboard("{Escape}");
    expect(opener).toHaveFocus();
  });
});
