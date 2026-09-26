/*
 * Generated-form logic (spec §5.5 *Configuration schemas*, §21.24).
 *
 * Everything the Devices screen knows about a driver's configuration is in
 * its schema, so this module is deliberately free of any driver name. It
 * turns a schema plus a stored configuration into form values, decides which
 * fields are visible, validates as a convenience, and builds the body to
 * submit. Client-side validation is not the boundary: the API validates
 * against the same schema and answers `validation_failed` with per-field
 * detail, because a scripted caller bypasses the form.
 */
import type { DependsOn, SchemaField } from "./types";

/** What a control holds. Everything but a checkbox keeps its text as typed. */
export type FormValue = string | boolean;

export type FormValues = Record<string, FormValue>;

/** A password already stored comes back as this and never as the secret (§6.10). */
export const SECRET_SENTINEL = { set: true };

export function isSecretSet(value: unknown): boolean {
  return typeof value === "object" && value !== null && (value as { set?: unknown }).set === true;
}

/** An unusable pattern is the server's problem, not a reason to refuse what was typed. */
function compile(pattern: string): RegExp | null {
  try {
    return new RegExp(pattern);
  } catch {
    return null;
  }
}

const PORT_MIN = 1;
const PORT_MAX = 65535;

function asFormValue(field: SchemaField, raw: unknown): FormValue {
  if (field.type === "bool") {
    if (typeof raw === "boolean") return raw;
    if (raw === "true") return true;
    if (raw === "false") return false;
    return typeof field.default === "boolean" ? field.default : false;
  }
  // An encrypted value is never returned, so its control always starts empty.
  if (field.encrypted || isSecretSet(raw)) return "";
  if (raw === null || raw === undefined) {
    return field.default === null || field.default === undefined ? "" : String(field.default);
  }
  return String(raw);
}

/** Form values for `schema`, seeded from `stored` and then from each field's default. */
export function initialValues(schema: readonly SchemaField[], stored: Record<string, unknown> = {}): FormValues {
  const values: FormValues = {};
  for (const field of schema) values[field.key] = asFormValue(field, stored[field.key]);
  return values;
}

/** Which encrypted fields already hold a value — the "set" indicator (§21.24). */
export function secretsSet(schema: readonly SchemaField[], stored: Record<string, unknown> = {}): Set<string> {
  const set = new Set<string>();
  for (const field of schema) {
    if (!field.encrypted) continue;
    const value = stored[field.key];
    if (isSecretSet(value) || (typeof value === "string" && value.length > 0)) set.add(field.key);
  }
  return set;
}

function matches(dependency: DependsOn, values: FormValues, context: Record<string, unknown>): boolean {
  const own = Object.prototype.hasOwnProperty.call(values, dependency.field)
    ? values[dependency.field]
    : context[dependency.field];
  if (typeof dependency.equals === "boolean" || typeof own === "boolean") {
    return String(own) === String(dependency.equals);
  }
  return String(own ?? "") === String(dependency.equals ?? "");
}

/**
 * `depends_on` decides visibility (§5.5): serial fields appear only while
 * serial is selected. `context` carries values from outside this schema —
 * the chosen transport type, which the transport picker owns.
 */
export function isVisible(field: SchemaField, values: FormValues, context: Record<string, unknown> = {}): boolean {
  return !field.depends_on || matches(field.depends_on, values, context);
}

export function visibleFields(
  schema: readonly SchemaField[],
  values: FormValues,
  context: Record<string, unknown> = {},
): SchemaField[] {
  return schema.filter((field) => isVisible(field, values, context));
}

export interface ValidateOptions {
  /** Encrypted fields the operator has typed into; an untouched one is "unchanged". */
  touched?: ReadonlySet<string>;
  /** Encrypted fields that already hold a stored value. */
  secrets?: ReadonlySet<string>;
  context?: Record<string, unknown>;
}

function numericBounds(field: SchemaField): { min: number; max: number } {
  const min = field.min ?? (field.type === "port" ? PORT_MIN : Number.NEGATIVE_INFINITY);
  const max = field.max ?? (field.type === "port" ? PORT_MAX : Number.POSITIVE_INFINITY);
  return { min, max };
}

/** Per-field messages for the visible fields, keyed by field key. Empty means valid. */
export function validate(
  schema: readonly SchemaField[],
  values: FormValues,
  options: ValidateOptions = {},
): Record<string, string> {
  const { touched = new Set<string>(), secrets = new Set<string>(), context = {} } = options;
  const errors: Record<string, string> = {};
  for (const field of visibleFields(schema, values, context)) {
    const value = values[field.key];
    if (field.type === "bool") continue;
    const text = typeof value === "string" ? value.trim() : "";

    if (!text) {
      // An untouched encrypted field means "leave it as it is" (§6.10), so a
      // required password that is already set is not missing.
      const alreadySet = field.encrypted && secrets.has(field.key) && !touched.has(field.key);
      if (field.required && !alreadySet) errors[field.key] = "This is required";
      continue;
    }

    if (field.type === "int" || field.type === "port") {
      if (!/^-?\d+$/.test(text)) {
        errors[field.key] = "Enter a whole number";
        continue;
      }
      const parsed = Number(text);
      const { min, max } = numericBounds(field);
      if (parsed < min) errors[field.key] = `Must be ${min} or more`;
      else if (parsed > max) errors[field.key] = `Must be ${max} or less`;
      continue;
    }

    if (field.type === "enum") {
      const allowed = (field.options ?? []).map((option) => option.value);
      if (allowed.length && !allowed.includes(text)) errors[field.key] = "Choose one of the listed options";
      continue;
    }

    if (field.pattern) {
      const expression = compile(field.pattern);
      if (expression && !expression.test(text)) errors[field.key] = "This does not match the required format";
    }
  }
  return errors;
}

export interface SubmitOptions {
  touched?: ReadonlySet<string>;
  context?: Record<string, unknown>;
}

/**
 * The values to send. Hidden fields are left out — the serial settings of a
 * driver switched to Ethernet are not part of its configuration — and an
 * untouched encrypted field is omitted, which the API reads as unchanged.
 */
export function toSubmit(
  schema: readonly SchemaField[],
  values: FormValues,
  options: SubmitOptions = {},
): Record<string, unknown> {
  const { touched = new Set<string>(), context = {} } = options;
  const body: Record<string, unknown> = {};
  for (const field of visibleFields(schema, values, context)) {
    const value = values[field.key];
    if (field.type === "bool") {
      body[field.key] = value === true;
      continue;
    }
    const text = typeof value === "string" ? value.trim() : "";
    if (field.encrypted && !touched.has(field.key)) continue;
    if (!text) {
      if (field.required) body[field.key] = "";
      continue;
    }
    body[field.key] = field.type === "int" || field.type === "port" ? Number(text) : text;
  }
  return body;
}

/** Defaults a transport contributes when it is chosen (§5.5 `TRANSPORT_DEFAULTS`). */
export function withTransportDefaults(
  schema: readonly SchemaField[],
  defaults: Record<string, unknown>,
  stored: Record<string, unknown> = {},
): FormValues {
  const seeded: Record<string, unknown> = { ...defaults, ...stored };
  return initialValues(schema, seeded);
}
