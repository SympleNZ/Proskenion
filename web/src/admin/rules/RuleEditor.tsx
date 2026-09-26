/*
 * The rule editor (spec §21.17 *Rule editor*, §8.2-§8.9). WHEN / ONLY WHEN /
 * THEN, exactly as the mock-up draws it: the trigger picker offers the four
 * sources of §8.3 and the fields below it change with the source; one
 * optional guard, from the fixed list of §8.5; the three action kinds of
 * §8.9. Match types are disabled according to the chosen address's data
 * type (`dpt.ts` mirrors `proskenion/rules/model.py`'s classing), with the
 * reason shown — but the server's own `422` on save is still the final word
 * (§8.7's constraints included), and its field errors land here by name.
 */
import { useState, type FormEvent } from "react";

import type { Device } from "@/admin/devices/types";
import { Banner } from "@/components/ui/Banner";
import { Button } from "@/components/ui/Button";
import { Field, Input } from "@/components/ui/Input";
import { Checkbox, Select } from "@/components/ui/Select";
import { Sheet, SheetContent } from "@/components/ui/Sheet";
import type { LightingGroup } from "@/lighting/types";
import { saveFormOnShortcut } from "@/lib/keyboard";

import { cronPreview } from "./cron";
import { allowedMatchTypes, dptClass, MATCH_TYPE_LABELS, matchTypeDisabledReason } from "./dpt";
import {
  CONNECTION_STATES,
  formatDeviceStateGuard,
  formatExternalControlGuard,
  formatTimeWindow,
  GUARD_TYPE_LABELS,
  parseDeviceStateGuard,
  parseExternalControlGuard,
  parseTimeWindow,
  STATE_ALIASES,
} from "./guards";
import { BINDING_FORCES_MULTIPLIER, NOTIFY_IS_LOG_ONLY, SCHEDULE_BEHAVIOUR, SURFACE_NOT_FIRING } from "./notices";
import {
  ACTION_TYPES,
  DEFAULT_DEBOUNCE_MS,
  GUARD_TYPES,
  MATCH_TYPES,
  TRIGGER_TYPES,
  type ActionType,
  type GuardType,
  type KnxAddress,
  type MatchType,
  type Rule,
  type RuleInput,
  type SceneSummary,
  type TriggerType,
} from "./types";

/** Several disabled match types often share one reason (every boolean-only reason is the same
 * sentence) — grouped so the same explanation is not repeated line after line. */
function groupDisabledMatchTypes(types: readonly MatchType[], dpt: string): { reason: string; labels: string[] }[] {
  const byReason = new Map<string, string[]>();
  for (const type of types) {
    const reason = matchTypeDisabledReason(type, dpt);
    if (reason === null) continue;
    const labels = byReason.get(reason) ?? [];
    labels.push(MATCH_TYPE_LABELS[type]);
    byReason.set(reason, labels);
  }
  return [...byReason.entries()].map(([reason, labels]) => ({ reason, labels }));
}

const TRIGGER_LABELS: Readonly<Record<TriggerType, string>> = {
  knx: "KNX telegram",
  schedule: "Schedule",
  surface: "Control surface",
  device_state: "Device state",
};

const ACTION_LABELS: Readonly<Record<ActionType, string>> = {
  run_scene: "Run scene",
  lighting_group: "Lighting group",
  notify: "Notify",
};

interface FormState {
  name: string;
  notes: string;
  enabled: boolean;
  trigger_type: TriggerType;
  knx_address_id: number | null;
  match_type: MatchType;
  match_value: string;
  match_value_max: string;
  debounce_ms: string;
  cron: string;
  trigger_device_id: number | null;
  trigger_state: string;
  trigger_for_ms: string;
  guard_type: GuardType | "";
  guard_window_start: string;
  guard_window_end: string;
  guard_external_active: boolean;
  guard_device_id: number | null;
  guard_device_state: string;
  action_type: ActionType;
  scene_id: number | null;
  lighting_group_id: number | null;
  on_level: string;
  off_level: string;
  fade_ms: string;
  message: string;
}

function initialState(rule: Rule | undefined): FormState {
  const window = parseTimeWindow(rule?.guard_type === "time_window" ? rule.guard_value : null);
  const deviceGuard = parseDeviceStateGuard(rule?.guard_type === "device_state" ? rule.guard_value : null);
  return {
    name: rule?.name ?? "",
    notes: rule?.notes ?? "",
    enabled: rule?.enabled ?? true,
    trigger_type: rule?.trigger_type ?? "knx",
    knx_address_id: rule?.knx_address_id ?? null,
    match_type: rule?.match_type ?? "any",
    match_value: rule?.match_value ?? "",
    match_value_max: rule?.match_value_max ?? "",
    debounce_ms: String(rule?.debounce_ms ?? DEFAULT_DEBOUNCE_MS),
    cron: rule?.cron ?? "",
    trigger_device_id: rule?.trigger_device_id ?? null,
    trigger_state: rule?.trigger_state ?? "",
    trigger_for_ms: rule?.trigger_for_ms !== null && rule?.trigger_for_ms !== undefined ? String(rule.trigger_for_ms) : "",
    guard_type: rule?.guard_type ?? "",
    guard_window_start: window.start,
    guard_window_end: window.end,
    guard_external_active: rule?.guard_type === "external_control" ? parseExternalControlGuard(rule.guard_value) : true,
    guard_device_id: deviceGuard.deviceId,
    guard_device_state: deviceGuard.state,
    action_type: rule?.action_type ?? "lighting_group",
    scene_id: rule?.scene_id ?? null,
    lighting_group_id: rule?.lighting_group_id ?? null,
    on_level: rule?.on_level !== null && rule?.on_level !== undefined ? String(rule.on_level) : "100",
    off_level: rule?.off_level !== null && rule?.off_level !== undefined ? String(rule.off_level) : "0",
    fade_ms: rule?.fade_ms !== null && rule?.fade_ms !== undefined ? String(rule.fade_ms) : "0",
    message: rule?.message ?? "",
  };
}

function toNumberOrNull(text: string): number | null {
  const trimmed = text.trim();
  if (trimmed === "") return null;
  const n = Number(trimmed);
  return Number.isFinite(n) ? n : null;
}

/** The body the server expects, with every field the current trigger/action does not use left `null` (§8.9). */
function buildInput(form: FormState): RuleInput {
  const guardValue =
    form.guard_type === "time_window"
      ? formatTimeWindow({ start: form.guard_window_start, end: form.guard_window_end })
      : form.guard_type === "external_control"
        ? formatExternalControlGuard(form.guard_external_active)
        : form.guard_type === "device_state"
          ? formatDeviceStateGuard({ deviceId: form.guard_device_id, state: form.guard_device_state })
          : null;

  return {
    name: form.name,
    enabled: form.enabled,
    sort_order: 0, // reordering is a list-level action (`RulesTab.tsx`), never edited here
    notes: form.notes.trim() === "" ? null : form.notes,
    trigger_type: form.trigger_type,
    knx_address_id: form.trigger_type === "knx" ? form.knx_address_id : null,
    match_type: form.trigger_type === "knx" ? form.match_type : "any",
    match_value: form.trigger_type === "knx" && form.match_type !== "any" ? form.match_value : null,
    match_value_max: form.trigger_type === "knx" && form.match_type === "range" ? form.match_value_max : null,
    debounce_ms: form.trigger_type === "knx" ? (toNumberOrNull(form.debounce_ms) ?? DEFAULT_DEBOUNCE_MS) : null,
    cron: form.trigger_type === "schedule" ? form.cron : null,
    trigger_device_id: form.trigger_type === "device_state" ? form.trigger_device_id : null,
    trigger_state: form.trigger_type === "device_state" ? form.trigger_state : null,
    trigger_for_ms: form.trigger_type === "device_state" ? toNumberOrNull(form.trigger_for_ms) : null,
    guard_type: form.guard_type === "" ? null : form.guard_type,
    guard_value: guardValue,
    action_type: form.action_type,
    scene_id: form.action_type === "run_scene" ? form.scene_id : null,
    lighting_group_id: form.action_type === "lighting_group" ? form.lighting_group_id : null,
    on_level: form.action_type === "lighting_group" ? toNumberOrNull(form.on_level) : null,
    off_level: form.action_type === "lighting_group" ? toNumberOrNull(form.off_level) : null,
    fade_ms: form.action_type === "lighting_group" ? toNumberOrNull(form.fade_ms) : null,
    message: form.action_type === "notify" ? form.message : null,
  };
}

export interface RuleEditorProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  rule?: Rule | undefined;
  knxAddresses: readonly KnxAddress[];
  scenes: readonly SceneSummary[];
  lightingGroups: readonly LightingGroup[];
  devices: readonly Device[];
  saving: boolean;
  deleting?: boolean;
  onSave: (input: RuleInput) => Promise<void>;
  onDelete?: (() => void) | undefined;
  fieldErrors: Record<string, string>;
  onErrorsHandled: () => void;
}

const STATE_SUGGESTIONS = [...CONNECTION_STATES, ...STATE_ALIASES];

function DeviceStateFields({
  idPrefix,
  deviceId,
  state,
  devices,
  onDeviceChange,
  onStateChange,
  error,
  stateError,
}: {
  idPrefix: string;
  deviceId: number | null;
  state: string;
  devices: readonly Device[];
  onDeviceChange: (id: number | null) => void;
  onStateChange: (state: string) => void;
  error?: string | undefined;
  stateError?: string | undefined;
}) {
  return (
    <>
      <Field label="Device" htmlFor={`${idPrefix}-device`} helpId="rules.trigger.device-state.device" error={error} errorId={`${idPrefix}-device-error`}>
        <Select
          id={`${idPrefix}-device`}
          value={deviceId ?? ""}
          aria-invalid={error ? true : undefined}
          onChange={(event) => onDeviceChange(event.currentTarget.value ? Number(event.currentTarget.value) : null)}
        >
          <option value="">Choose a device…</option>
          {devices.map((device) => (
            <option key={device.id} value={device.id}>
              {device.name}
            </option>
          ))}
        </Select>
      </Field>
      <Field label="State" htmlFor={`${idPrefix}-state`} helpId="rules.trigger.device-state.state" error={stateError} errorId={`${idPrefix}-state-error`}>
        <Input
          id={`${idPrefix}-state`}
          list={`${idPrefix}-state-options`}
          value={state}
          aria-invalid={stateError ? true : undefined}
          onChange={(event) => onStateChange(event.currentTarget.value)}
          placeholder="online"
        />
        <datalist id={`${idPrefix}-state-options`}>
          {STATE_SUGGESTIONS.map((s) => (
            <option key={s} value={s} />
          ))}
        </datalist>
        <p className="field-help">
          A device&apos;s connection status, e.g. online or offline, or a device&apos;s own state once its driver reports
          one (§8.3).
        </p>
      </Field>
    </>
  );
}

export function RuleEditor({
  open,
  onOpenChange,
  rule,
  knxAddresses,
  scenes,
  lightingGroups,
  devices,
  saving,
  deleting = false,
  onSave,
  onDelete,
  fieldErrors,
  onErrorsHandled,
}: RuleEditorProps) {
  // The caller keys this component by the rule being edited (`RulesTab.tsx`
  // passes `key={editing === "new" ? "new" : editing.id}`), so a fresh
  // instance mounts per rule and this lazy initialiser is the only reset
  // the form ever needs — no effect re-seeding state on every open.
  const [form, setForm] = useState<FormState>(() => initialState(rule));

  function set<K extends keyof FormState>(key: K, value: FormState[K]) {
    setForm((current) => ({ ...current, [key]: value }));
  }

  const idPrefix = rule ? `rule-${rule.id}` : "rule-new";
  const selectedAddress = knxAddresses.find((a) => a.id === form.knx_address_id);
  const cls = selectedAddress ? dptClass(selectedAddress.dpt) : null;
  const allowed = cls ? allowedMatchTypes(cls) : new Set(MATCH_TYPES);
  const incomingAddresses = knxAddresses.filter((a) => a.direction === "incoming");

  const isBinding = form.action_type === "lighting_group";
  const bindingMismatch = isBinding && (form.trigger_type !== "knx" || form.match_type !== "any");

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    onErrorsHandled();
    try {
      await onSave(buildInput(form));
    } catch {
      // Field errors are surfaced through `fieldErrors`; anything else is toasted by the caller.
    }
  }

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent title={rule ? rule.name : "Add rule"} side="right">
      <form className="device-form" noValidate onKeyDown={saveFormOnShortcut} onSubmit={(e) => void handleSubmit(e)}>
        <Field label="Name" htmlFor={`${idPrefix}-name`} helpId="rules.name" error={fieldErrors["name"]} errorId={`${idPrefix}-name-error`}>
          <Input
            id={`${idPrefix}-name`}
            value={form.name}
            aria-invalid={fieldErrors["name"] ? true : undefined}
            onChange={(event) => set("name", event.currentTarget.value)}
            required
          />
        </Field>

        <section className="device-section" aria-labelledby={`${idPrefix}-when`}>
          <h3 className="sect-label" id={`${idPrefix}-when`}>
            When
          </h3>

          <Field label="Trigger" htmlFor={`${idPrefix}-trigger`} helpId="rules.trigger" error={fieldErrors["trigger_type"]} errorId={`${idPrefix}-trigger-error`}>
            <Select
              id={`${idPrefix}-trigger`}
              value={form.trigger_type}
              onChange={(event) => set("trigger_type", event.currentTarget.value as TriggerType)}
            >
              {TRIGGER_TYPES.map((t) => (
                <option key={t} value={t}>
                  {TRIGGER_LABELS[t]}
                </option>
              ))}
            </Select>
          </Field>

          {form.trigger_type === "knx" ? (
            <>
              <Field
                label="Address"
                htmlFor={`${idPrefix}-address`}
                helpId="rules.trigger.knx.address"
                error={fieldErrors["knx_address_id"]}
                errorId={`${idPrefix}-address-error`}
              >
                <Select
                  id={`${idPrefix}-address`}
                  value={form.knx_address_id ?? ""}
                  aria-invalid={fieldErrors["knx_address_id"] ? true : undefined}
                  onChange={(event) => set("knx_address_id", event.currentTarget.value ? Number(event.currentTarget.value) : null)}
                >
                  <option value="">Choose an address…</option>
                  {incomingAddresses.map((address) => (
                    <option key={address.id} value={address.id}>
                      {address.name} — {address.group_address}
                    </option>
                  ))}
                </Select>
                {selectedAddress ? (
                  <p className="field-help technical">
                    DPT {selectedAddress.dpt} · {selectedAddress.direction}
                  </p>
                ) : null}
              </Field>

              <Field label="Match" htmlFor={`${idPrefix}-match`} helpId="rules.trigger.knx.match" error={fieldErrors["match_type"]} errorId={`${idPrefix}-match-error`}>
                <Select
                  id={`${idPrefix}-match`}
                  value={form.match_type}
                  onChange={(event) => set("match_type", event.currentTarget.value as MatchType)}
                >
                  {MATCH_TYPES.map((m) => (
                    <option key={m} value={m} disabled={cls !== null && !allowed.has(m)}>
                      {MATCH_TYPE_LABELS[m]}
                    </option>
                  ))}
                </Select>
                {selectedAddress
                  ? groupDisabledMatchTypes(MATCH_TYPES.filter((m) => !allowed.has(m)), selectedAddress.dpt).map(({ reason, labels }) => (
                      <p className="field-help" key={reason}>
                        {labels.join(", ")} {labels.length > 1 ? "are" : "is"} unavailable: {reason}
                      </p>
                    ))
                  : null}
              </Field>

              {form.match_type !== "any" ? (
                <Field
                  label={form.match_type === "range" ? "Value (lower)" : "Value"}
                  htmlFor={`${idPrefix}-match-value`}
                  helpId="rules.trigger.knx.match-value"
                  error={fieldErrors["match_value"]}
                  errorId={`${idPrefix}-match-value-error`}
                >
                  <Input
                    id={`${idPrefix}-match-value`}
                    value={form.match_value}
                    aria-invalid={fieldErrors["match_value"] ? true : undefined}
                    onChange={(event) => set("match_value", event.currentTarget.value)}
                  />
                </Field>
              ) : null}
              {form.match_type === "range" ? (
                <Field
                  label="Value (upper)"
                  htmlFor={`${idPrefix}-match-value-max`}
                  helpId="rules.trigger.knx.match-value-max"
                  error={fieldErrors["match_value_max"]}
                  errorId={`${idPrefix}-match-value-max-error`}
                >
                  <Input
                    id={`${idPrefix}-match-value-max`}
                    value={form.match_value_max}
                    aria-invalid={fieldErrors["match_value_max"] ? true : undefined}
                    onChange={(event) => set("match_value_max", event.currentTarget.value)}
                  />
                </Field>
              ) : null}

              <Field
                label="Debounce (ms)"
                htmlFor={`${idPrefix}-debounce`}
                helpId="rules.trigger.knx.debounce"
                error={fieldErrors["debounce_ms"]}
                errorId={`${idPrefix}-debounce-error`}
              >
                <Input
                  id={`${idPrefix}-debounce`}
                  type="number"
                  min={0}
                  value={form.debounce_ms}
                  aria-invalid={fieldErrors["debounce_ms"] ? true : undefined}
                  onChange={(event) => set("debounce_ms", event.currentTarget.value)}
                />
                <p className="field-help">
                  A repeat within this window is suppressed — a panel that sends on press and release does not
                  restart a fade (§8.4). KNX triggers only.
                </p>
              </Field>
            </>
          ) : null}

          {form.trigger_type === "schedule" ? (
            <>
              <Banner tone="info" title="When it runs">
                {SCHEDULE_BEHAVIOUR}
              </Banner>
              <Field label="Cron expression" htmlFor={`${idPrefix}-cron`} helpId="rules.trigger.schedule.cron" error={fieldErrors["cron"]} errorId={`${idPrefix}-cron-error`}>
                <Input
                  id={`${idPrefix}-cron`}
                  mono
                  value={form.cron}
                  aria-invalid={fieldErrors["cron"] ? true : undefined}
                  onChange={(event) => set("cron", event.currentTarget.value)}
                  placeholder="0 23 * * *"
                />
                <p className="field-help">{form.cron.trim() ? (cronPreview(form.cron) ?? "Five fields: minute hour day-of-month month day-of-week") : "Minute hour day-of-month month day-of-week, evaluated in Pacific/Auckland."}</p>
              </Field>
            </>
          ) : null}

          {form.trigger_type === "surface" ? (
            <Banner tone="info" title="Not fired yet">
              {SURFACE_NOT_FIRING}
            </Banner>
          ) : null}

          {form.trigger_type === "device_state" ? (
            <>
              <DeviceStateFields
                idPrefix={`${idPrefix}-trigger`}
                deviceId={form.trigger_device_id}
                state={form.trigger_state}
                devices={devices}
                onDeviceChange={(id) => set("trigger_device_id", id)}
                onStateChange={(state) => set("trigger_state", state)}
                error={fieldErrors["trigger_device_id"]}
                stateError={fieldErrors["trigger_state"]}
              />
              <Field
                label="Sustained for (ms)"
                htmlFor={`${idPrefix}-for-ms`}
                helpId="rules.trigger.device-state.sustained"
                error={fieldErrors["trigger_for_ms"]}
                errorId={`${idPrefix}-for-ms-error`}
              >
                <Input
                  id={`${idPrefix}-for-ms`}
                  type="number"
                  min={0}
                  value={form.trigger_for_ms}
                  aria-invalid={fieldErrors["trigger_for_ms"] ? true : undefined}
                  onChange={(event) => set("trigger_for_ms", event.currentTarget.value)}
                  placeholder="optional"
                />
                <p className="field-help">
                  Optional — the device must stay in this state this long before the rule fires, so a brief
                  reconnection does not fire an alert (§8.3).
                </p>
              </Field>
            </>
          ) : null}
        </section>

        <section className="device-section" aria-labelledby={`${idPrefix}-guard`}>
          <h3 className="sect-label" id={`${idPrefix}-guard`}>
            Only when <span className="field-note">(optional)</span>
          </h3>
          <Field label="Guard" htmlFor={`${idPrefix}-guard-type`} helpId="rules.guard.type" error={fieldErrors["guard_type"]} errorId={`${idPrefix}-guard-type-error`}>
            <Select
              id={`${idPrefix}-guard-type`}
              value={form.guard_type}
              onChange={(event) => set("guard_type", event.currentTarget.value as GuardType | "")}
            >
              <option value="">None</option>
              {GUARD_TYPES.map((g) => (
                <option key={g} value={g}>
                  {GUARD_TYPE_LABELS[g]}
                </option>
              ))}
            </Select>
            <p className="field-help">One guard, not a tree — anything more complex is a scene (§8.5).</p>
          </Field>

          {form.guard_type === "time_window" ? (
            <div className="flex gap-3">
              <Field label="From" htmlFor={`${idPrefix}-window-start`} helpId="rules.guard.time-window.from" error={fieldErrors["guard_value"]} errorId={`${idPrefix}-window-start-error`}>
                <Input
                  id={`${idPrefix}-window-start`}
                  type="time"
                  value={form.guard_window_start}
                  onChange={(event) => set("guard_window_start", event.currentTarget.value)}
                />
              </Field>
              <Field label="To" htmlFor={`${idPrefix}-window-end`} helpId="rules.guard.time-window.to" error={undefined} errorId={`${idPrefix}-window-end-error`}>
                <Input
                  id={`${idPrefix}-window-end`}
                  type="time"
                  value={form.guard_window_end}
                  onChange={(event) => set("guard_window_end", event.currentTarget.value)}
                />
              </Field>
            </div>
          ) : null}
          {form.guard_type === "external_control" ? (
            <Field
              label="External control"
              htmlFor={`${idPrefix}-guard-external`}
              helpId="rules.guard.external-control"
              error={fieldErrors["guard_value"]}
              errorId={`${idPrefix}-guard-external-error`}
            >
              <Select
                id={`${idPrefix}-guard-external`}
                value={form.guard_external_active ? "active" : "inactive"}
                onChange={(event) => set("guard_external_active", event.currentTarget.value === "active")}
              >
                <option value="active">Only when active</option>
                <option value="inactive">Only when inactive</option>
              </Select>
            </Field>
          ) : null}
          {form.guard_type === "device_state" ? (
            <DeviceStateFields
              idPrefix={`${idPrefix}-guard`}
              deviceId={form.guard_device_id}
              state={form.guard_device_state}
              devices={devices}
              onDeviceChange={(id) => set("guard_device_id", id)}
              onStateChange={(state) => set("guard_device_state", state)}
              error={fieldErrors["guard_value"]}
              stateError={fieldErrors["guard_value"]}
            />
          ) : null}
        </section>

        <section className="device-section" aria-labelledby={`${idPrefix}-then`}>
          <h3 className="sect-label" id={`${idPrefix}-then`}>
            Then
          </h3>
          <Field label="Action" htmlFor={`${idPrefix}-action`} helpId="rules.action.type" error={fieldErrors["action_type"]} errorId={`${idPrefix}-action-error`}>
            <Select id={`${idPrefix}-action`} value={form.action_type} onChange={(event) => set("action_type", event.currentTarget.value as ActionType)}>
              {ACTION_TYPES.map((a) => (
                <option key={a} value={a}>
                  {ACTION_LABELS[a]}
                </option>
              ))}
            </Select>
            <p className="field-help">Anything needing steps or delays is a scene — rules never chain (§8.1, §8.7).</p>
          </Field>

          {form.action_type === "run_scene" ? (
            <Field label="Scene" htmlFor={`${idPrefix}-scene`} helpId="rules.action.scene" error={fieldErrors["scene_id"]} errorId={`${idPrefix}-scene-error`}>
              <Select
                id={`${idPrefix}-scene`}
                value={form.scene_id ?? ""}
                aria-invalid={fieldErrors["scene_id"] ? true : undefined}
                onChange={(event) => set("scene_id", event.currentTarget.value ? Number(event.currentTarget.value) : null)}
              >
                <option value="">Choose a scene…</option>
                {scenes.map((scene) => (
                  <option key={scene.id} value={scene.id}>
                    {scene.name}
                  </option>
                ))}
              </Select>
            </Field>
          ) : null}

          {form.action_type === "lighting_group" ? (
            <>
              {bindingMismatch ? (
                <Banner tone="warning" title="A binding needs a knx trigger with match any">
                  The telegram&apos;s value selects the on or off level, so this needs trigger “KNX telegram” and
                  match “Any value” (§8.2).
                </Banner>
              ) : null}
              <Field
                label="Group"
                htmlFor={`${idPrefix}-group`}
                helpId="rules.action.lighting-group.group"
                error={fieldErrors["lighting_group_id"]}
                errorId={`${idPrefix}-group-error`}
              >
                <Select
                  id={`${idPrefix}-group`}
                  value={form.lighting_group_id ?? ""}
                  aria-invalid={fieldErrors["lighting_group_id"] ? true : undefined}
                  onChange={(event) => set("lighting_group_id", event.currentTarget.value ? Number(event.currentTarget.value) : null)}
                >
                  <option value="">Choose a group…</option>
                  {lightingGroups.map((group) => (
                    <option key={group.id} value={group.id}>
                      {group.name}
                    </option>
                  ))}
                </Select>
              </Field>
              <div className="flex gap-3">
                <Field label="On level (%)" htmlFor={`${idPrefix}-on`} helpId="rules.action.lighting-group.on" error={fieldErrors["on_level"]} errorId={`${idPrefix}-on-error`}>
                  <Input
                    id={`${idPrefix}-on`}
                    type="number"
                    min={0}
                    max={100}
                    step={0.1}
                    value={form.on_level}
                    onChange={(event) => set("on_level", event.currentTarget.value)}
                  />
                </Field>
                <Field label="Off level (%)" htmlFor={`${idPrefix}-off`} helpId="rules.action.lighting-group.off" error={fieldErrors["off_level"]} errorId={`${idPrefix}-off-error`}>
                  <Input
                    id={`${idPrefix}-off`}
                    type="number"
                    min={0}
                    max={100}
                    step={0.1}
                    value={form.off_level}
                    onChange={(event) => set("off_level", event.currentTarget.value)}
                  />
                </Field>
                <Field label="Fade (ms)" htmlFor={`${idPrefix}-fade`} helpId="rules.action.lighting-group.fade" error={fieldErrors["fade_ms"]} errorId={`${idPrefix}-fade-error`}>
                  <Input
                    id={`${idPrefix}-fade`}
                    type="number"
                    min={0}
                    value={form.fade_ms}
                    onChange={(event) => set("fade_ms", event.currentTarget.value)}
                  />
                </Field>
              </div>
              <Banner tone="info">{BINDING_FORCES_MULTIPLIER}</Banner>
            </>
          ) : null}

          {form.action_type === "notify" ? (
            <>
              <Banner tone="info" title="Log only, for now">
                {NOTIFY_IS_LOG_ONLY}
              </Banner>
              <Field label="Message" htmlFor={`${idPrefix}-message`} helpId="rules.action.notify.message" error={fieldErrors["message"]} errorId={`${idPrefix}-message-error`}>
                <Input
                  id={`${idPrefix}-message`}
                  value={form.message}
                  aria-invalid={fieldErrors["message"] ? true : undefined}
                  onChange={(event) => set("message", event.currentTarget.value)}
                />
              </Field>
            </>
          ) : null}
        </section>

        <Checkbox
          id={`${idPrefix}-enabled`}
          label="Enabled"
          checked={form.enabled}
          onChange={(event) => set("enabled", event.currentTarget.checked)}
        />
        {fieldErrors["notes"] !== undefined ? (
          <Banner tone="danger">{fieldErrors["notes"]}</Banner>
        ) : null}
        <Field label="Notes" htmlFor={`${idPrefix}-notes`} helpId="rules.notes" error={undefined} errorId={`${idPrefix}-notes-error`}>
          <Input id={`${idPrefix}-notes`} value={form.notes} onChange={(event) => set("notes", event.currentTarget.value)} />
        </Field>

        <div className="device-actions">
          <Button type="submit" variant="primary" helpId="rules.save" loading={saving}>
            Save
          </Button>
          {onDelete ? (
            <Button type="button" variant="destructive" helpId="rules.delete" loading={deleting} onClick={onDelete}>
              Delete
            </Button>
          ) : null}
        </div>
      </form>
      </SheetContent>
    </Sheet>
  );
}
