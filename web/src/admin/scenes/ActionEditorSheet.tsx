/*
 * Add or edit one action (spec §8.12, §21.16). `dmx` (with snapshot
 * capture), `knx`, `projector_power`, `projector_input`, `hdmi_source` and
 * the three mixer domains all have working forms in this build.
 *
 * Field errors come from the server's 422 `validation_failed` (§16.1) and are
 * shown next to the field they name, exactly as `useDeviceForm` does for the
 * Devices screen — client-side checks here are a convenience, not the
 * boundary.
 */
import { useId, useState, type ReactNode } from "react";

import { useFaderLaw, useMixerChannels, useMixerDeskScenes, useMixerState } from "@/admin/mixer/api";
import { ApiError } from "@/api/client";
import { presentError } from "@/api/errors";
import { lawFaderScale } from "@/components/fader/FaderScale";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Checkbox, Select } from "@/components/ui/Select";
import { Sheet, SheetContent } from "@/components/ui/Sheet";
import { FieldLabel, HelpButton } from "@/help/HelpButton";
import { formatDb, type FaderLawPoint } from "@/lib/faderLaw";
import { saveFormOnShortcut } from "@/lib/keyboard";
import { useProjectorState } from "@/projector/api";
import { useHdmiState } from "@/video/api";

import { useCaptureSnapshot, useCreateAction, useKnxAddresses, useUpdateAction } from "./api";
import { DomainPicker } from "./DomainPicker";
import type { Action, ActionFields, Domain, DomainAvailability, DmxSnapshot, KnxSource } from "./types";

/** One radio option, in `AddressForm`'s established `.radio`/`.radio-mark` shape (§21.19).
 * `disabled` keeps an option visible but unusable — §5.5's "disabled with a
 * reason, never hidden" applied to one choice rather than the whole tile
 * (see `DomainPicker`, and `admin/mixer/DeskScenesSection.tsx` for the same
 * treatment of a row). */
function RadioOption({
  id,
  name,
  checked,
  onSelect,
  disabled,
  children,
}: {
  id: string;
  name: string;
  checked: boolean;
  onSelect: () => void;
  disabled?: boolean;
  children: ReactNode;
}) {
  return (
    <label className="radio" htmlFor={id} aria-disabled={disabled || undefined}>
      <span className="radio-mark" aria-hidden="true" />
      <input type="radio" id={id} name={name} checked={checked} disabled={disabled} onChange={onSelect} />
      <span>{children}</span>
    </label>
  );
}

/**
 * A mixer level: dB, or "Off" as its own deliberate choice — `null`, never a
 * number, and never merely the law's lowest published dB (§5.5, B41). Mirrors
 * `admin/mixer/HirerCeilingControl.tsx`'s own "no ceiling"/law-positioned
 * shape, with the wording this is actually asking for.
 */
function MixerLevelField({
  id,
  value,
  onChange,
  law,
}: {
  id: string;
  value: number | null;
  onChange: (next: number | null) => void;
  law: readonly FaderLawPoint[];
}) {
  const offId = useId();
  const isOff = value === null;
  const scale = law.length > 0 ? lawFaderScale(law) : null;

  function toggleOff(off: boolean): void {
    if (off) {
      onChange(null);
      return;
    }
    onChange(value ?? (scale ? scale.min : 0));
  }

  return (
    <div className="field schema-field">
      <div className="field-label-row">
        <span className="field-label" id={`${id}-label`}>
          Level
        </span>
        <HelpButton id="scenes.action.mixer.level" />
      </div>
      <Checkbox id={offId} label="Off" checked={isOff} onChange={(event) => toggleOff(event.currentTarget.checked)} />
      {!isOff && value !== null ? (
        scale ? (
          <div className="connection-row">
            <input
              id={id}
              type="range"
              aria-labelledby={`${id}-label`}
              min={0}
              max={1000}
              step={1}
              value={Math.round(scale.toPosition(value) * 1000)}
              onChange={(event) => onChange(scale.fromPosition(Number(event.currentTarget.value) / 1000))}
            />
            <span className="technical">{scale.format(value)}</span>
          </div>
        ) : (
          <div className="connection-row">
            <input
              id={id}
              className="input input-mono"
              type="number"
              step={0.1}
              aria-labelledby={`${id}-label`}
              value={value}
              onChange={(event) => onChange(Number(event.currentTarget.value))}
            />
            <span className="technical">{formatDb(value)}</span>
          </div>
        )
      ) : null}
    </div>
  );
}

export interface ActionEditorSheetProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  sceneId: number;
  /** Present when editing; absent when adding a new action. */
  action?: Action | undefined;
  /** New actions default into whatever delay group the "+ Add" was pressed from. */
  defaultDelayMs: number;
  defaultSortOrder: number;
  availability: readonly DomainAvailability[] | undefined;
}

function snapshotSummary(snapshot: DmxSnapshot | null): string {
  const ids = Object.keys(snapshot ?? {});
  if (ids.length === 0) return "No look captured yet";
  const levels = ids
    .slice(0, 6)
    .map((id) => `#${id}: ${(snapshot?.[id]?.level ?? 0).toFixed(1)}%`)
    .join(", ");
  const more = ids.length > 6 ? `, +${ids.length - 6} more` : "";
  return `${ids.length} channel${ids.length === 1 ? "" : "s"} — ${levels}${more}`;
}

export function ActionEditorSheet({
  open,
  onOpenChange,
  sceneId,
  action,
  defaultDelayMs,
  defaultSortOrder,
  availability,
}: ActionEditorSheetProps) {
  const create = useCreateAction();
  const update = useUpdateAction();
  const captureSnapshot = useCaptureSnapshot();
  const knxAddresses = useKnxAddresses();
  // Fetched lazily by TanStack Query regardless of which domain is picked
  // (both are cheap, cached `GET`s the operator views already warm) rather
  // than gated behind `domain === "projector_input" | "hdmi_source"`, so
  // switching between the two never shows an empty list while a query that
  // could have started on open is still in flight.
  const projectorState = useProjectorState();
  const hdmiState = useHdmiState();
  const mixerState = useMixerState();
  const mixerChannels = useMixerChannels();
  const mixerDeskScenes = useMixerDeskScenes();
  const mixerFaderLaw = useFaderLaw(mixerState.data?.device_id ?? null);

  const [domain, setDomain] = useState<Domain | null>((action?.domain as Domain | undefined) ?? null);
  const [delayMs, setDelayMs] = useState(action?.delay_ms ?? defaultDelayMs);
  const [dmxSnapshot, setDmxSnapshot] = useState<DmxSnapshot | null>(action?.dmx_snapshot ?? null);
  const [dmxFadeMs, setDmxFadeMs] = useState(action?.dmx_fade_ms ?? 0);
  const [knxAddressId, setKnxAddressId] = useState<number | null>(action?.knx_address_id ?? null);
  const [knxSource, setKnxSource] = useState<KnxSource>((action?.knx_source as KnxSource | undefined) ?? "literal");
  const [knxValue, setKnxValue] = useState(action?.knx_value ?? "");
  const [knxScale, setKnxScale] = useState(action?.knx_scale ?? "");
  const [projectorPower, setProjectorPower] = useState<"on" | "off">(
    (action?.projector_power as "on" | "off" | undefined) ?? "on",
  );
  const [projectorInput, setProjectorInput] = useState<string | null>(action?.projector_input ?? null);
  const [hdmiDestination, setHdmiDestination] = useState<number | null>(action?.hdmi_destination ?? null);
  // `null` is "Venue default" (§13.5) — the pre-selected choice for a new action.
  const [hdmiInputId, setHdmiInputId] = useState<number | null>(action?.hdmi_input_id ?? null);
  // `null` is the mixer's own "Venue Default" desk scene (§13.5) — the
  // pre-selected choice for a new action, exactly as `hdmiInputId` above.
  const [mixerSceneId, setMixerSceneId] = useState<number | null>(action?.mixer_scene_id ?? null);
  const [mixerChannelId, setMixerChannelId] = useState<number | null>(action?.mixer_channel_id ?? null);
  const [mixerDb, setMixerDb] = useState<number | null>(action?.mixer_db ?? null);
  const [mixerMuted, setMixerMuted] = useState<boolean>(action?.mixer_muted ?? false);
  const [errors, setErrors] = useState<Readonly<Record<string, string>>>({});

  // Reset to the target action (or a blank new one) whenever the sheet opens
  // on a different action — never mid-edit, which would blow away typing.
  // Comparing an identity key during render, rather than in an effect, is
  // React's documented way to adjust state when a prop that identifies "which
  // thing" changes.
  const openKey = open ? (action?.id ?? "new") : null;
  const [syncedKey, setSyncedKey] = useState<number | "new" | null>(null);
  if (openKey !== syncedKey) {
    setSyncedKey(openKey);
    if (openKey !== null) {
      setDomain((action?.domain as Domain | undefined) ?? null);
      setDelayMs(action?.delay_ms ?? defaultDelayMs);
      setDmxSnapshot(action?.dmx_snapshot ?? null);
      setDmxFadeMs(action?.dmx_fade_ms ?? 0);
      setKnxAddressId(action?.knx_address_id ?? null);
      setKnxSource((action?.knx_source as KnxSource | undefined) ?? "literal");
      setKnxValue(action?.knx_value ?? "");
      setKnxScale(action?.knx_scale ?? "");
      setProjectorPower((action?.projector_power as "on" | "off" | undefined) ?? "on");
      setProjectorInput(action?.projector_input ?? null);
      setHdmiDestination(action?.hdmi_destination ?? null);
      setHdmiInputId(action?.hdmi_input_id ?? null);
      setMixerSceneId(action?.mixer_scene_id ?? null);
      setMixerChannelId(action?.mixer_channel_id ?? null);
      setMixerDb(action?.mixer_db ?? null);
      setMixerMuted(action?.mixer_muted ?? false);
      setErrors({});
    }
  }

  function buildBody(): ActionFields | null {
    if (!domain) return null;
    const base = {
      sort_order: action?.sort_order ?? defaultSortOrder,
      delay_ms: delayMs,
      domain,
      knx_address_id: null,
      knx_value: null,
      knx_source: "literal",
      knx_scale: null,
      dmx_snapshot: null,
      dmx_fade_ms: null,
      mixer_scene_id: null,
      mixer_channel_id: null,
      mixer_db: null,
      mixer_muted: null,
      projector_power: null,
      projector_input: null,
      hdmi_destination: null,
      hdmi_input_id: null,
      device_id: action?.device_id ?? null,
    } satisfies ActionFields;
    if (domain === "dmx") {
      return { ...base, dmx_snapshot: dmxSnapshot, dmx_fade_ms: dmxFadeMs };
    }
    if (domain === "knx") {
      return {
        ...base,
        knx_address_id: knxAddressId,
        knx_source: knxSource,
        knx_value: knxSource === "literal" ? (knxValue || null) : null,
        knx_scale: knxScale.trim() === "" ? null : knxScale,
      };
    }
    if (domain === "projector_power") {
      return { ...base, projector_power: projectorPower };
    }
    if (domain === "projector_input") {
      return { ...base, projector_input: projectorInput };
    }
    if (domain === "hdmi_source") {
      return { ...base, hdmi_destination: hdmiDestination, hdmi_input_id: hdmiInputId };
    }
    if (domain === "mixer_recall") {
      return { ...base, mixer_scene_id: mixerSceneId };
    }
    if (domain === "mixer_fader") {
      return { ...base, mixer_channel_id: mixerChannelId, mixer_db: mixerDb };
    }
    if (domain === "mixer_mute") {
      return { ...base, mixer_channel_id: mixerChannelId, mixer_muted: mixerMuted };
    }
    return base;
  }

  function submit() {
    const body = buildBody();
    if (!body) {
      setErrors({ domain: "Choose an action type" });
      return;
    }
    const onError = (error: unknown) => {
      if (error instanceof ApiError && error.code === "validation_failed") {
        const presentation = presentError(error);
        if (presentation?.kind === "inline") setErrors(presentation.fields);
        return;
      }
      presentError(error);
    };
    const onSuccess = () => onOpenChange(false);
    if (action) {
      update.mutate({ sceneId, actionId: action.id, version: action.updated_at, body }, { onSuccess, onError });
    } else {
      create.mutate({ sceneId, body }, { onSuccess, onError });
    }
  }

  const outgoing = (knxAddresses.data ?? []).filter((address) => address.direction !== "incoming");
  const saving = create.isPending || update.isPending;

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent title={action ? "Edit action" : "Add action"} description="Actions in the same delay group fire together (§8.13).">
        <form
          className="sheet-body"
          noValidate
          onKeyDown={saveFormOnShortcut}
          onSubmit={(event) => {
            event.preventDefault();
            submit();
          }}
        >
          {!action ? <DomainPicker availability={availability} selected={domain} onSelect={setDomain} /> : null}
          {errors["domain"] ? (
            <div className="field-error" role="alert" aria-live="assertive">
              <span>{errors["domain"]}</span>
            </div>
          ) : null}

          <div className="field schema-field">
            <FieldLabel htmlFor="action-delay" help="scenes.action.delay">
              Delay (ms)
            </FieldLabel>
            <Input
              id="action-delay"
              type="number"
              min={0}
              value={delayMs}
              onChange={(event) => setDelayMs(Math.max(0, Number(event.currentTarget.value) || 0))}
            />
            <div className="field-error" role="alert" aria-live="assertive">
              {errors["delay_ms"] ? <span>{errors["delay_ms"]}</span> : null}
            </div>
          </div>

          {domain === "dmx" ? (
            <>
              <div className="field schema-field">
                <div className="field-label-row">
                  <span className="field-label" id="action-dmx-capture-label">
                    Look
                  </span>
                  <HelpButton id="scenes.action.dmx.capture" />
                </div>
                <Button
                  type="button"
                  variant="secondary"
                  aria-describedby="action-dmx-capture-label"
                  loading={captureSnapshot.isPending}
                  onClick={() =>
                    captureSnapshot.mutate(undefined, {
                      onSuccess: (result) => setDmxSnapshot(result.snapshot),
                      onError: (error) => void presentError(error),
                    })
                  }
                >
                  Capture current look
                </Button>
                <div className="snapshot-summary" role="status">
                  {snapshotSummary(dmxSnapshot)}
                </div>
                <div className="field-error" role="alert" aria-live="assertive">
                  {errors["dmx_snapshot"] ? <span>{errors["dmx_snapshot"]}</span> : null}
                </div>
              </div>
              <div className="field schema-field">
                <FieldLabel htmlFor="action-fade" help="scenes.action.dmx.fade">
                  Fade (ms) — a non-zero fade shows as &quot;DMX fade&quot; in the timeline
                </FieldLabel>
                <Input
                  id="action-fade"
                  type="number"
                  min={0}
                  value={dmxFadeMs}
                  onChange={(event) => setDmxFadeMs(Math.max(0, Number(event.currentTarget.value) || 0))}
                />
              </div>
            </>
          ) : null}

          {domain === "knx" ? (
            <>
              <div className="field schema-field">
                <FieldLabel htmlFor="action-knx-address" help="scenes.action.knx.address">
                  Address
                </FieldLabel>
                <Select
                  id="action-knx-address"
                  value={knxAddressId ?? ""}
                  onChange={(event) => setKnxAddressId(event.currentTarget.value ? Number(event.currentTarget.value) : null)}
                >
                  <option value="">Choose an address…</option>
                  {outgoing.map((address) => (
                    <option key={address.id} value={address.id}>
                      {address.group_address} — {address.name}
                    </option>
                  ))}
                </Select>
                <div className="field-error" role="alert" aria-live="assertive">
                  {errors["knx_address_id"] ? <span>{errors["knx_address_id"]}</span> : null}
                </div>
              </div>

              <div className="field schema-field">
                <FieldLabel htmlFor="action-knx-source" help="scenes.action.knx.source">
                  Value
                </FieldLabel>
                <Select id="action-knx-source" value={knxSource} onChange={(event) => setKnxSource(event.currentTarget.value as KnxSource)}>
                  <option value="literal">A literal value</option>
                  <option value="trigger_value">The trigger&apos;s value (optionally scaled)</option>
                </Select>
              </div>

              {knxSource === "literal" ? (
                <div className="field schema-field">
                  <FieldLabel htmlFor="action-knx-value" help="scenes.action.knx.literal">
                    Literal value
                  </FieldLabel>
                  <Input id="action-knx-value" value={knxValue} onChange={(event) => setKnxValue(event.currentTarget.value)} />
                  <div className="field-error" role="alert" aria-live="assertive">
                    {errors["knx_value"] ? <span>{errors["knx_value"]}</span> : null}
                  </div>
                </div>
              ) : (
                <div className="field schema-field">
                  <FieldLabel htmlFor="action-knx-scale" help="scenes.action.knx.scale">
                    Scale (optional — numeric addresses only)
                  </FieldLabel>
                  <Input id="action-knx-scale" value={knxScale} onChange={(event) => setKnxScale(event.currentTarget.value)} placeholder="e.g. 2.55" />
                  <div className="field-error" role="alert" aria-live="assertive">
                    {errors["knx_scale"] ? <span>{errors["knx_scale"]}</span> : null}
                  </div>
                </div>
              )}
            </>
          ) : null}

          {domain === "projector_power" ? (
            <fieldset className="field schema-field">
              <legend className="field-label">Power</legend>
              <HelpButton id="scenes.action.projector.power" />
              <RadioOption
                id="action-projector-power-on"
                name="action-projector-power"
                checked={projectorPower === "on"}
                onSelect={() => setProjectorPower("on")}
              >
                On
              </RadioOption>
              <RadioOption
                id="action-projector-power-off"
                name="action-projector-power"
                checked={projectorPower === "off"}
                onSelect={() => setProjectorPower("off")}
              >
                Off
              </RadioOption>
              <div className="field-error" role="alert" aria-live="assertive">
                {errors["projector_power"] ? <span>{errors["projector_power"]}</span> : null}
              </div>
            </fieldset>
          ) : null}

          {domain === "projector_input" ? (
            <fieldset className="field schema-field">
              <legend className="field-label">
                Input — rejected, not queued, while the projector is warming, cooling or off (§8.13)
              </legend>
              <HelpButton id="scenes.action.projector.input" />
              {(projectorState.data?.inputs ?? []).map((option) => (
                <RadioOption
                  key={option.ref}
                  id={`action-projector-input-${option.ref}`}
                  name="action-projector-input"
                  checked={projectorInput === option.ref}
                  onSelect={() => setProjectorInput(option.ref)}
                >
                  {option.label}
                </RadioOption>
              ))}
              {(projectorState.data?.inputs.length ?? 0) === 0 ? (
                <p className="text-fg-muted text-xs">No projector inputs are known yet.</p>
              ) : null}
              <div className="field-error" role="alert" aria-live="assertive">
                {errors["projector_input"] ? <span>{errors["projector_input"]}</span> : null}
              </div>
            </fieldset>
          ) : null}

          {domain === "hdmi_source" ? (
            <>
              <div className="field schema-field">
                <FieldLabel htmlFor="action-hdmi-destination" help="scenes.action.hdmi.destination">
                  Destination
                </FieldLabel>
                <Select
                  id="action-hdmi-destination"
                  value={hdmiDestination ?? ""}
                  onChange={(event) =>
                    setHdmiDestination(event.currentTarget.value ? Number(event.currentTarget.value) : null)
                  }
                >
                  <option value="">Choose a destination…</option>
                  {(hdmiState.data?.destinations ?? []).map((destination) => (
                    <option key={destination.id} value={destination.id}>
                      {destination.name}
                    </option>
                  ))}
                </Select>
                <div className="field-error" role="alert" aria-live="assertive">
                  {errors["hdmi_destination"] ? <span>{errors["hdmi_destination"]}</span> : null}
                </div>
              </div>

              <fieldset className="field schema-field">
                <legend className="field-label">Source</legend>
                <HelpButton id="scenes.action.hdmi.source" />
                <RadioOption
                  id="action-hdmi-input-default"
                  name="action-hdmi-input"
                  checked={hdmiInputId === null}
                  onSelect={() => setHdmiInputId(null)}
                >
                  Venue default
                </RadioOption>
                {(hdmiState.data?.inputs ?? []).map((input) => (
                  <RadioOption
                    key={input.id}
                    id={`action-hdmi-input-${input.id}`}
                    name="action-hdmi-input"
                    checked={hdmiInputId === input.id}
                    onSelect={() => setHdmiInputId(input.id)}
                  >
                    {input.name}
                  </RadioOption>
                ))}
                <div className="field-error" role="alert" aria-live="assertive">
                  {errors["hdmi_input_id"] ? <span>{errors["hdmi_input_id"]}</span> : null}
                </div>
              </fieldset>
            </>
          ) : null}

          {domain === "mixer_recall" ? (
            <fieldset className="field schema-field">
              <legend className="field-label">Desk scene</legend>
              <HelpButton id="scenes.action.mixer.recall" />
              {mixerState.data && !mixerState.data.capabilities.scene_recall ? (
                <p className="field-help">
                  The configured mixer&apos;s driver does not support scene recall (§15.6).
                </p>
              ) : null}
              <RadioOption
                id="action-mixer-recall-default"
                name="action-mixer-recall"
                checked={mixerSceneId === null}
                onSelect={() => setMixerSceneId(null)}
                disabled={mixerState.data ? !mixerState.data.capabilities.scene_recall : false}
              >
                Venue Default
              </RadioOption>
              {(mixerDeskScenes.data ?? []).map((scene) => (
                <RadioOption
                  key={scene.id}
                  id={`action-mixer-recall-${scene.id}`}
                  name="action-mixer-recall"
                  checked={mixerSceneId === scene.id}
                  onSelect={() => setMixerSceneId(scene.id)}
                  disabled={mixerState.data ? !mixerState.data.capabilities.scene_recall : false}
                >
                  {scene.name}
                  {scene.is_venue_default ? " (Venue Default)" : ""}
                </RadioOption>
              ))}
              <div className="field-error" role="alert" aria-live="assertive">
                {errors["mixer_scene_id"] ? <span>{errors["mixer_scene_id"]}</span> : null}
              </div>
            </fieldset>
          ) : null}

          {domain === "mixer_fader" ? (
            <>
              <div className="field schema-field">
                <FieldLabel htmlFor="action-mixer-fader-channel" help="scenes.action.mixer.channel">
                  Channel
                </FieldLabel>
                <Select
                  id="action-mixer-fader-channel"
                  value={mixerChannelId ?? ""}
                  onChange={(event) =>
                    setMixerChannelId(event.currentTarget.value ? Number(event.currentTarget.value) : null)
                  }
                >
                  <option value="">Choose a channel…</option>
                  {(mixerChannels.data ?? []).map((channel) => (
                    <option key={channel.id} value={channel.id}>
                      {channel.name}
                    </option>
                  ))}
                </Select>
                <div className="field-error" role="alert" aria-live="assertive">
                  {errors["mixer_channel_id"] ? <span>{errors["mixer_channel_id"]}</span> : null}
                </div>
              </div>
              <MixerLevelField
                id="action-mixer-fader-level"
                value={mixerDb}
                onChange={setMixerDb}
                law={mixerFaderLaw.data?.fader_law ?? []}
              />
              <div className="field-error" role="alert" aria-live="assertive">
                {errors["mixer_db"] ? <span>{errors["mixer_db"]}</span> : null}
              </div>
            </>
          ) : null}

          {domain === "mixer_mute" ? (
            <>
              <div className="field schema-field">
                <FieldLabel htmlFor="action-mixer-mute-channel" help="scenes.action.mixer.channel">
                  Channel
                </FieldLabel>
                <Select
                  id="action-mixer-mute-channel"
                  value={mixerChannelId ?? ""}
                  onChange={(event) =>
                    setMixerChannelId(event.currentTarget.value ? Number(event.currentTarget.value) : null)
                  }
                >
                  <option value="">Choose a channel…</option>
                  {(mixerChannels.data ?? []).map((channel) => (
                    <option key={channel.id} value={channel.id}>
                      {channel.name}
                    </option>
                  ))}
                </Select>
                <div className="field-error" role="alert" aria-live="assertive">
                  {errors["mixer_channel_id"] ? <span>{errors["mixer_channel_id"]}</span> : null}
                </div>
              </div>
              <fieldset className="field schema-field">
                <legend className="field-label">Mute — always absolute, never a toggle (§7.3)</legend>
                <HelpButton id="scenes.action.mixer.mute" />
                <RadioOption
                  id="action-mixer-mute-on"
                  name="action-mixer-mute"
                  checked={mixerMuted}
                  onSelect={() => setMixerMuted(true)}
                >
                  Muted
                </RadioOption>
                <RadioOption
                  id="action-mixer-mute-off"
                  name="action-mixer-mute"
                  checked={!mixerMuted}
                  onSelect={() => setMixerMuted(false)}
                >
                  Not muted
                </RadioOption>
                <div className="field-error" role="alert" aria-live="assertive">
                  {errors["mixer_muted"] ? <span>{errors["mixer_muted"]}</span> : null}
                </div>
              </fieldset>
            </>
          ) : null}

          <div className="dialog-actions">
            <Button type="button" variant="secondary" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" variant="primary" helpId="scenes.action.save" loading={saving} disabled={!domain}>
              {action ? "Save action" : "Add action"}
            </Button>
          </div>
        </form>
      </SheetContent>
    </Sheet>
  );
}
