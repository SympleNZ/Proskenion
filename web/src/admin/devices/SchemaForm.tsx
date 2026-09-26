/*
 * The generated form (spec §21.24, §5.5 *Configuration schemas*).
 *
 * Every control here is chosen by a field's `type`, and the vocabulary is
 * closed: string, int, bool, enum, port, password, device_path, host. Nothing
 * below names a driver, so a driver that ships later renders with no change
 * — which is what SchemaForm.test.tsx proves against a fabricated schema.
 */
import type { ReactNode } from "react";

import { Input } from "@/components/ui/Input";
import { Checkbox, Select } from "@/components/ui/Select";
import { cn } from "@/lib/utils";

import type { FormValues } from "./schema";
import { visibleFields } from "./schema";
import type { SchemaField } from "./types";

export interface SchemaFormProps {
  schema: readonly SchemaField[];
  values: FormValues;
  /** Per-field messages, keyed by field key. Validation fires on save, not on blur (§21.27). */
  errors?: Record<string, string>;
  /** Encrypted fields that already hold a stored value — the "set" indicator. */
  secrets?: ReadonlySet<string>;
  /** Encrypted fields typed into; an untouched one is omitted from the submit (§6.10). */
  touched?: ReadonlySet<string>;
  /** Values from outside this schema that `depends_on` may name, e.g. the transport type. */
  context?: Record<string, unknown>;
  /** Namespaces the control ids so two schemas can render on one card. */
  idPrefix: string;
  disabled?: boolean;
  /** Replaces the control for one field — the serial picker stands in for device_path. */
  renderControl?: (field: SchemaField, control: ControlContext) => ReactNode | undefined;
  onChange: (key: string, value: string | boolean) => void;
}

export interface ControlContext {
  id: string;
  describedBy: string | undefined;
  invalid: boolean;
  disabled: boolean;
  value: string | boolean;
  onChange: (value: string | boolean) => void;
}

function controlFor(field: SchemaField, context: ControlContext): ReactNode {
  const { id, describedBy, invalid, disabled, value } = context;
  const text = typeof value === "string" ? value : "";
  const common = {
    id,
    disabled,
    "aria-describedby": describedBy,
    "aria-invalid": invalid ? (true as const) : undefined,
    required: field.required || undefined,
  };

  switch (field.type) {
    case "bool":
      return (
        <Checkbox
          {...common}
          label={field.label}
          checked={value === true}
          onChange={(event) => context.onChange(event.currentTarget.checked)}
        />
      );

    case "enum":
      return (
        <Select {...common} value={text} onChange={(event) => context.onChange(event.currentTarget.value)}>
          {!field.required && <option value="">Not set</option>}
          {(field.options ?? []).map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </Select>
      );

    case "int":
    case "port":
      return (
        <Input
          {...common}
          type="number"
          inputMode="numeric"
          mono
          min={field.min ?? (field.type === "port" ? 1 : undefined)}
          max={field.max ?? (field.type === "port" ? 65535 : undefined)}
          value={text}
          onChange={(event) => context.onChange(event.currentTarget.value)}
        />
      );

    case "password":
      return (
        <Input
          {...common}
          type="password"
          autoComplete="new-password"
          value={text}
          onChange={(event) => context.onChange(event.currentTarget.value)}
        />
      );

    case "host":
    case "device_path":
      return (
        <Input
          {...common}
          type="text"
          mono
          autoComplete="off"
          spellCheck={false}
          value={text}
          onChange={(event) => context.onChange(event.currentTarget.value)}
        />
      );

    case "string":
      return (
        <Input
          {...common}
          type="text"
          autoComplete="off"
          value={text}
          onChange={(event) => context.onChange(event.currentTarget.value)}
        />
      );
  }
}

export function SchemaForm({
  schema,
  values,
  errors = {},
  secrets = new Set<string>(),
  touched = new Set<string>(),
  context = {},
  idPrefix,
  disabled = false,
  renderControl,
  onChange,
}: SchemaFormProps) {
  const fields = visibleFields(schema, values, context);
  if (fields.length === 0) return null;

  return (
    <div className="schema-form">
      {fields.map((field) => {
        const id = `${idPrefix}-${field.key}`;
        const helpId = field.help ? `${id}-help` : undefined;
        const noteId = field.encrypted ? `${id}-secret` : undefined;
        const errorId = `${id}-error`;
        const error = errors[field.key];
        const describedBy = [helpId, noteId, error ? errorId : undefined].filter(Boolean).join(" ") || undefined;
        const control: ControlContext = {
          id,
          describedBy,
          invalid: Boolean(error),
          disabled,
          value: values[field.key] ?? (field.type === "bool" ? false : ""),
          onChange: (value) => onChange(field.key, value),
        };
        const custom = renderControl?.(field, control);
        const secretSet = field.encrypted && secrets.has(field.key);

        return (
          <div className={cn("field schema-field")} key={field.key} data-field={field.key} data-type={field.type}>
            {field.type === "bool" ? null : (
              <label className="field-label" htmlFor={id}>
                {field.label}
                {field.required ? (
                  <span className="field-required" aria-hidden="true">
                    {" "}
                    (required)
                  </span>
                ) : null}
              </label>
            )}
            {custom ?? controlFor(field, control)}
            {secretSet ? (
              <p className="field-note" id={noteId}>
                {touched.has(field.key)
                  ? "This will replace the stored value."
                  : "A value is set. Leave this blank to keep it."}
              </p>
            ) : null}
            {field.help ? (
              <p className="field-help" id={helpId}>
                {field.help}
              </p>
            ) : null}
            <div className="field-error" id={errorId} role="alert" aria-live="assertive">
              {error ? <span>{error}</span> : null}
            </div>
          </div>
        );
      })}
    </div>
  );
}
