/*
 * The generated form (spec §21.24, §5.5).
 *
 * The driver in these tests does not exist. That is the whole assertion:
 * adding a driver must need no frontend change, so a schema the interface has
 * never seen has to render every one of the eight field types, honour
 * `depends_on`, and leave an untouched encrypted field out of the submit.
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it } from "vitest";

import { FABRICATED_SCHEMA } from "./fixtures";
import { initialValues, secretsSet, toSubmit, validate, type FormValues } from "./schema";
import { SchemaForm } from "./SchemaForm";
import type { SchemaField } from "./types";

function Harness({ schema = FABRICATED_SCHEMA, stored = {} }: { schema?: SchemaField[]; stored?: Record<string, unknown> }) {
  const [values, setValues] = useState<FormValues>(() => initialValues(schema, stored));
  const [touched, setTouched] = useState<ReadonlySet<string>>(new Set<string>());
  return (
    <SchemaForm
      schema={schema}
      values={values}
      secrets={secretsSet(schema, stored)}
      touched={touched}
      idPrefix="test"
      onChange={(key, value) => {
        setValues((current) => ({ ...current, [key]: value }));
        setTouched((current) => new Set([...current, key]));
      }}
    />
  );
}

describe("SchemaForm — a fabricated driver schema", () => {
  it("renders a control for every type in the closed vocabulary, with no frontend change", () => {
    render(<Harness />);

    expect(screen.getByLabelText(/^Label/)).toHaveAttribute("type", "text");
    expect(screen.getByLabelText(/^Retries/)).toHaveAttribute("type", "number");
    expect(screen.getByLabelText("Verbose logging")).toHaveAttribute("type", "checkbox");
    expect(screen.getByLabelText(/^Mode/).tagName).toBe("SELECT");
    const port = screen.getByLabelText(/^Control port/);
    expect(port).toHaveAttribute("type", "number");
    expect(port).toHaveAttribute("max", "65535");
    expect(screen.getByLabelText(/^API token/)).toHaveAttribute("type", "password");
    expect(screen.getByLabelText(/^Device/)).toHaveAttribute("type", "text");
    expect(screen.getByLabelText(/^IP address/)).toHaveAttribute("type", "text");
  });

  it("renders help as inline help, marks required fields and seeds defaults", () => {
    render(<Harness />);
    expect(screen.getByText("Written into the log beside every command")).toBeInTheDocument();
    expect(screen.getByLabelText(/^Retries/)).toHaveValue(3);
    expect(screen.getByLabelText(/^Control port/)).toHaveValue(5000);
    expect(screen.getByLabelText(/^Mode/)).toHaveValue("alpha");
    expect(screen.getByText(/IP address/).textContent).toContain("(required)");
  });

  it("shows and hides a dependent field as depends_on is satisfied", () => {
    render(<Harness />);
    expect(screen.queryByLabelText(/^Beta window/)).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText(/^Mode/), { target: { value: "beta" } });
    expect(screen.getByLabelText(/^Beta window/)).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText(/^Mode/), { target: { value: "alpha" } });
    expect(screen.queryByLabelText(/^Beta window/)).not.toBeInTheDocument();
  });

  it("renders an encrypted field write-only, with a set indicator and no stored value", () => {
    render(<Harness stored={{ api_token: { set: true } }} />);
    const token = screen.getByLabelText(/^API token/);
    expect(token).toHaveValue("");
    expect(screen.getByText("A value is set. Leave this blank to keep it.")).toBeInTheDocument();

    fireEvent.change(token, { target: { value: "a-new-token" } });
    expect(screen.getByText("This will replace the stored value.")).toBeInTheDocument();
  });
});

describe("schema — values, validation and the submitted body", () => {
  const stored = { api_token: { set: true }, label: "House", address: "10.2.30.71" };

  it("omits an untouched encrypted field from the submit, and includes a touched one", () => {
    const values = initialValues(FABRICATED_SCHEMA, stored);

    const untouched = toSubmit(FABRICATED_SCHEMA, values, { touched: new Set() });
    expect(untouched).not.toHaveProperty("api_token");
    expect(untouched).toMatchObject({ label: "House", address: "10.2.30.71", control_port: 5000, verbose: false });

    const typed = toSubmit(
      FABRICATED_SCHEMA,
      { ...values, api_token: "fresh-secret" },
      { touched: new Set(["api_token"]) },
    );
    expect(typed["api_token"]).toBe("fresh-secret");
  });

  it("leaves a hidden field out of the submit entirely", () => {
    const values = { ...initialValues(FABRICATED_SCHEMA, stored), mode: "alpha" };
    expect(toSubmit(FABRICATED_SCHEMA, values)).not.toHaveProperty("beta_window");
    expect(toSubmit(FABRICATED_SCHEMA, { ...values, mode: "beta" })).toHaveProperty("beta_window", 5);
  });

  it("coerces int and port to numbers and leaves everything else as text", () => {
    const values = { ...initialValues(FABRICATED_SCHEMA, stored), retries: "7", control_port: "51325" };
    const body = toSubmit(FABRICATED_SCHEMA, values);
    expect(body["retries"]).toBe(7);
    expect(body["control_port"]).toBe(51325);
    expect(body["label"]).toBe("House");
  });

  it("validates required, bounds and pattern as a convenience, never as the boundary", () => {
    const empty = initialValues(FABRICATED_SCHEMA, {});
    const errors = validate(FABRICATED_SCHEMA, empty);
    expect(errors["label"]).toBe("This is required");
    expect(errors["address"]).toBe("This is required");
    // An encrypted field that is already set is not missing when left blank.
    expect(validate(FABRICATED_SCHEMA, empty, { secrets: new Set(["api_token"]) })["api_token"]).toBeUndefined();

    const outOfRange = validate(FABRICATED_SCHEMA, { ...empty, retries: "99", control_port: "70000" });
    expect(outOfRange["retries"]).toBe("Must be 10 or less");
    expect(outOfRange["control_port"]).toBe("Must be 65535 or less");
    expect(validate(FABRICATED_SCHEMA, { ...empty, retries: "two" })["retries"]).toBe("Enter a whole number");

    const patterned: SchemaField[] = [
      { key: "code", type: "string", label: "Code", required: false, pattern: "^[A-Z]{3}$" },
    ];
    expect(validate(patterned, { code: "abc" })["code"]).toBe("This does not match the required format");
    expect(validate(patterned, { code: "ABC" })["code"]).toBeUndefined();
  });
});
