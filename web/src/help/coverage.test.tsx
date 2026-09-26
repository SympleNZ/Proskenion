/*
 * The coverage checker itself (spec §19.1). Each admin screen's own test
 * file renders that screen and asserts `findMissingHelp` returns nothing —
 * see e.g. `admin/devices/DevicesScreen.test.tsx`. This file proves the
 * checker actually catches what it is supposed to: the negative case a
 * newly added, unhelped control must fail.
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { Field } from "@/components/ui/Input";
import { Checkbox } from "@/components/ui/Select";

import { describeMissing, findMissingHelp } from "./coverage";

describe("findMissingHelp", () => {
  it("flags a labelled field with no help id — the negative case", () => {
    const { container } = render(
      <Field label="A brand new field" htmlFor="new-field" errorId="new-field-error">
        <input id="new-field" />
      </Field>,
    );
    const missing = findMissingHelp(container);
    expect(missing).toEqual([{ kind: "field", description: "A brand new field" }]);
  });

  it("does not flag a field once it carries a help id", () => {
    const { container } = render(
      <Field label="Name" htmlFor="named-field" errorId="named-field-error" helpId="devices.name">
        <input id="named-field" />
      </Field>,
    );
    expect(findMissingHelp(container)).toEqual([]);
  });

  it("flags a primary action button with no help id — the negative case", () => {
    render(<Button variant="primary">Do the thing</Button>);
    const missing = findMissingHelp(document.body);
    expect(missing).toEqual([{ kind: "button", description: "Do the thing" }]);
  });

  it("does not flag a primary button once it carries a help id", () => {
    render(
      <Button variant="primary" helpId="devices.save">
        Save
      </Button>,
    );
    expect(findMissingHelp(document.body)).toEqual([]);
  });

  it("never requires help on a secondary button", () => {
    render(<Button variant="secondary">Cancel</Button>);
    expect(findMissingHelp(document.body)).toEqual([]);
  });

  it("flags a destructive button with no help id — the negative case (26 Sep milestone audit: Delete image)", () => {
    render(<Button variant="destructive">Delete</Button>);
    expect(findMissingHelp(document.body)).toEqual([{ kind: "button", description: "Delete" }]);
  });

  it("does not flag a destructive button once it carries a help id", () => {
    render(
      <Button variant="destructive" helpId="devices.name">
        Delete
      </Button>,
    );
    expect(findMissingHelp(document.body)).toEqual([]);
  });

  it("flags a plain secondary button that opens a ConfirmDialog — the negative case (26 Sep milestone audit: Restart, Reboot)", () => {
    render(
      <Button variant="secondary" confirmTrigger>
        Restart application
      </Button>,
    );
    const missing = findMissingHelp(document.body);
    expect(missing).toEqual([{ kind: "button", description: "Restart application" }]);
  });

  it("does not flag a confirmTrigger button once it carries a help id, and gives it aria-haspopup", () => {
    render(
      <Button variant="secondary" confirmTrigger helpId="devices.save">
        Restart application
      </Button>,
    );
    expect(findMissingHelp(document.body)).toEqual([]);
    expect(screen.getByRole("button", { name: "Restart application" })).toHaveAttribute("aria-haspopup", "dialog");
  });

  it("flags a card with a control and no help anywhere in it — the negative case (26 Sep milestone audit: Snapshots, Images, Restart/Reboot, Debug logging)", () => {
    render(
      <Card title="Debug logging">
        <Checkbox id="debug-core" label="core" />
      </Card>,
    );
    expect(findMissingHelp(document.body)).toEqual([{ kind: "card", description: "Debug logging" }]);
  });

  it("does not flag a card once it carries a help trigger anywhere, even not on the control itself", () => {
    render(
      <Card
        title={
          <span>
            Debug logging
            <Button variant="ghost" size="icon" helpId="devices.name" />
          </span>
        }
      >
        <Checkbox id="debug-core-2" label="core" />
      </Card>,
    );
    expect(findMissingHelp(document.body)).toEqual([]);
  });

  it("does not require a card-level help trigger for a card whose only bare input is a hidden file picker — the visible button beside it explains itself", () => {
    render(
      <Card title="Restore">
        <input type="file" className="sr-only" aria-label="Upload a backup file" />
        <Button variant="secondary">Upload a backup file</Button>
      </Card>,
    );
    expect(findMissingHelp(document.body)).toEqual([]);
  });

  it("does not require a card-level help trigger for a card whose only control is a plain button — an Edit row opening an already-covered sheet", () => {
    render(
      <Card title="Group 1">
        <Button variant="secondary">Edit</Button>
      </Card>,
    );
    expect(findMissingHelp(document.body)).toEqual([]);
  });

  it("does not require help on a driver-declared schema field (already covered by its own field.help)", () => {
    const { container } = render(
      <div className="field schema-field" data-field="host" data-type="host">
        <label className="field-label" htmlFor="host">
          Host
        </label>
        <input id="host" />
      </div>,
    );
    expect(findMissingHelp(container)).toEqual([]);
  });

  it("describeMissing lists every missing control on its own line", () => {
    const { container } = render(
      <>
        <Field label="One" htmlFor="one" errorId="one-error">
          <input id="one" />
        </Field>
        <Button variant="primary">Two</Button>
      </>,
    );
    expect(describeMissing(findMissingHelp(container))).toBe("field: One\nbutton: Two");
  });
});

describe("HelpButton (accessibility)", () => {
  it("has an accessible name naming what it explains and opens on click", async () => {
    const events = userEvent.setup();
    render(
      <Field label="Name" htmlFor="acc-field" errorId="acc-field-error" helpId="devices.name">
        <input id="acc-field" />
      </Field>,
    );
    const trigger = screen.getByRole("button", { name: "Help: Name" });
    expect(trigger).toBeInTheDocument();
    await events.click(trigger);
    expect(await screen.findByText(/What this device is called/)).toBeInTheDocument();
  });

  it("opens with the keyboard (Enter) and Escape closes it, returning focus to the trigger", async () => {
    const events = userEvent.setup();
    render(
      <Field label="Name" htmlFor="kbd-field" errorId="kbd-field-error" helpId="devices.name">
        <input id="kbd-field" />
      </Field>,
    );
    const trigger = screen.getByRole("button", { name: "Help: Name" });
    trigger.focus();
    await events.keyboard("{Enter}");
    expect(await screen.findByText(/What this device is called/)).toBeInTheDocument();
    await events.keyboard("{Escape}");
    expect(screen.queryByText(/What this device is called/)).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });
});
