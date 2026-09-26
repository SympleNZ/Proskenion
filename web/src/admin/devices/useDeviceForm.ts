/*
 * One device's editing state (spec §5.5, §21.24).
 *
 * Two schemas, two sections: the transport supplies addressing, the driver
 * supplies protocol settings, and they are kept apart because they come from
 * different places. Switching the transport rebuilds only the addressing
 * fields — a PJLink projector moved from Ethernet to Serial keeps its
 * password.
 *
 * Error keys are dotted — `transport.host`, `driver.midi_channel` — because
 * that is how the API returns them, so a server refusal lands on the same
 * field a client-side refusal would.
 */
import { useCallback, useEffect, useRef, useState } from "react";

import { initialValues, secretsSet, toSubmit, validate, withTransportDefaults, type FormValues } from "./schema";
import type { DeviceConfig, Driver, SchemaField, TransportInfo } from "./types";

export type Block = "transport" | "driver";

export interface DeviceFormState {
  transportType: string;
  transport: TransportInfo | undefined;
  transportSchema: readonly SchemaField[];
  driverSchema: readonly SchemaField[];
  values: Readonly<Record<Block, FormValues>>;
  secrets: Readonly<Record<Block, ReadonlySet<string>>>;
  touched: Readonly<Record<Block, ReadonlySet<string>>>;
  errors: Record<string, string>;
  dirty: boolean;
  setValue: (block: Block, key: string, value: string | boolean) => void;
  chooseTransport: (type: string) => void;
  setErrors: (errors: Record<string, string>) => void;
  /** Per-field messages, dotted. Empty means the form is ready to send. */
  validateAll: () => Record<string, string>;
  /** The `{transport, driver}` body of §5.5, with untouched secrets left out. */
  buildConfig: () => DeviceConfig;
  /** Discard the edits and take `config` as the new starting point. */
  reset: (config: DeviceConfig) => void;
}

function blockOf(config: DeviceConfig, block: Block): Record<string, unknown> {
  const value = config[block];
  return value && typeof value === "object" ? (value as Record<string, unknown>) : {};
}

function pickTransport(driver: Driver | undefined, type: string | undefined): TransportInfo | undefined {
  if (!driver) return undefined;
  return driver.transports.find((candidate) => candidate.type === type) ?? driver.transports[0];
}

function dotted(block: Block, errors: Record<string, string>): Record<string, string> {
  return Object.fromEntries(Object.entries(errors).map(([key, message]) => [`${block}.${key}`, message]));
}

export function stripBlock(block: Block, errors: Record<string, string>): Record<string, string> {
  const out: Record<string, string> = {};
  const prefix = `${block}.`;
  for (const [key, message] of Object.entries(errors)) {
    if (key.startsWith(prefix)) out[key.slice(prefix.length)] = message;
  }
  return out;
}

/**
 * `form.errors` entries that name a field neither section of `DeviceForm`
 * renders a control for — `transport.type` is the field that motivated this:
 * it is chosen through the Transport picker, not a `config_schema` entry, so
 * a refusal naming it used to land nowhere and the sheet showed nothing
 * (docs/phase-1-milestone.md, defect 2). `extraKnownKeys` lets a caller name
 * fields it renders itself outside `DeviceForm` — every current caller has a
 * "Name" field, for instance.
 */
export function unmatchedErrors(
  form: Pick<DeviceFormState, "errors" | "transportSchema" | "driverSchema">,
  extraKnownKeys: readonly string[] = [],
): Record<string, string> {
  const known = new Set<string>(extraKnownKeys);
  for (const field of form.transportSchema) known.add(`transport.${field.key}`);
  for (const field of form.driverSchema) known.add(`driver.${field.key}`);
  const out: Record<string, string> = {};
  for (const [key, message] of Object.entries(form.errors)) {
    if (!known.has(key)) out[key] = message;
  }
  return out;
}

export function useDeviceForm(driver: Driver | undefined, stored: DeviceConfig): DeviceFormState {
  const storedTransport = blockOf(stored, "transport");
  const storedType = typeof storedTransport["type"] === "string" ? (storedTransport["type"] as string) : undefined;

  const [transportType, setTransportType] = useState<string>(
    () => pickTransport(driver, storedType)?.type ?? storedType ?? "",
  );
  const transport = pickTransport(driver, transportType);
  const transportSchema: readonly SchemaField[] = transport?.config_schema ?? [];
  const driverSchema: readonly SchemaField[] = driver?.config_schema ?? [];

  const [values, setValues] = useState<Record<Block, FormValues>>(() => ({
    transport: withTransportDefaults(
      pickTransport(driver, storedType)?.config_schema ?? [],
      pickTransport(driver, storedType)?.defaults ?? {},
      storedTransport,
    ),
    driver: initialValues(driver?.config_schema ?? [], blockOf(stored, "driver")),
  }));
  const [touched, setTouched] = useState<Record<Block, Set<string>>>(() => ({
    transport: new Set<string>(),
    driver: new Set<string>(),
  }));
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [dirty, setDirty] = useState(false);

  // The devices step of the wizard mounts `AddDeviceSheet` before `GET
  // /drivers` has answered, so the very first render can have no driver at
  // all — the one-shot initialisers above then freeze on an empty form
  // forever, and it goes on to submit `transport.type: ""`. Re-derive the
  // starting values whenever the driver becomes known, or changes to a
  // different one, rather than only once at mount. Guarded by a ref so this
  // never fires on a render where the driver did not actually change — it
  // would otherwise clobber values the operator is mid-typing.
  const previousDriverKey = useRef<string | undefined>(driver?.key);
  useEffect(() => {
    if (driver === undefined || driver.key === previousDriverKey.current) return;
    previousDriverKey.current = driver.key;
    const chosen = pickTransport(driver, storedType);
    setTransportType(chosen?.type ?? storedType ?? "");
    setValues({
      transport: withTransportDefaults(chosen?.config_schema ?? [], chosen?.defaults ?? {}, storedTransport),
      driver: initialValues(driver.config_schema, blockOf(stored, "driver")),
    });
    setTouched({ transport: new Set<string>(), driver: new Set<string>() });
    setErrors({});
    setDirty(false);
  }, [driver, stored, storedTransport, storedType]);

  // Cheap enough to derive on every render, and `stored` is a fresh object
  // from the query cache each time, so memoising it would need a deep key.
  const secrets = {
    transport: secretsSet(pickTransport(driver, storedType)?.config_schema ?? [], storedTransport),
    driver: secretsSet(driver?.config_schema ?? [], blockOf(stored, "driver")),
  };

  const setValue = useCallback((block: Block, key: string, value: string | boolean) => {
    setValues((current) => ({ ...current, [block]: { ...current[block], [key]: value } }));
    setTouched((current) => {
      if (current[block].has(key)) return current;
      const next = new Set(current[block]);
      next.add(key);
      return { ...current, [block]: next };
    });
    setDirty(true);
  }, []);

  function chooseTransport(type: string): void {
    const next = driver?.transports.find((candidate) => candidate.type === type);
    if (!next) return;
    setTransportType(type);
    // Addressing is rebuilt from the new transport; the driver's own settings
    // are deliberately left alone (§21.24).
    setValues((current) => ({
      ...current,
      transport: withTransportDefaults(next.config_schema, next.defaults, type === storedType ? storedTransport : {}),
    }));
    setTouched((current) => ({ ...current, transport: new Set<string>() }));
    setDirty(true);
  }

  function validateAll(): Record<string, string> {
    const context = { type: transportType };
    const found = {
      ...dotted(
        "transport",
        validate(transportSchema, values.transport, {
          touched: touched.transport,
          secrets: secrets.transport,
          context,
        }),
      ),
      ...dotted(
        "driver",
        validate(driverSchema, values.driver, { touched: touched.driver, secrets: secrets.driver, context }),
      ),
    };
    setErrors(found);
    return found;
  }

  function buildConfig(): DeviceConfig {
    const context = { type: transportType };
    return {
      transport: {
        type: transportType,
        ...toSubmit(transportSchema, values.transport, { touched: touched.transport, context }),
      },
      driver: toSubmit(driverSchema, values.driver, { touched: touched.driver, context }),
    };
  }

  function reset(config: DeviceConfig): void {
    const nextTransport = blockOf(config, "transport");
    const type = typeof nextTransport["type"] === "string" ? (nextTransport["type"] as string) : undefined;
    const chosen = pickTransport(driver, type);
    setTransportType(chosen?.type ?? type ?? "");
    setValues({
      transport: withTransportDefaults(chosen?.config_schema ?? [], chosen?.defaults ?? {}, nextTransport),
      driver: initialValues(driver?.config_schema ?? [], blockOf(config, "driver")),
    });
    setTouched({ transport: new Set<string>(), driver: new Set<string>() });
    setErrors({});
    setDirty(false);
  }

  return {
    transportType,
    transport,
    transportSchema,
    driverSchema,
    values,
    secrets,
    touched,
    errors,
    dirty,
    setValue,
    chooseTransport,
    setErrors,
    validateAll,
    buildConfig,
    reset,
  };
}
