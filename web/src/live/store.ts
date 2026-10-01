/*
 * The live state store (spec §21.2, the counterpart to §5.6, and it carries the
 * same weight). Read this before writing any live-updating component: every
 * such surface depends on it and the failure modes are not obvious.
 *
 * Live state is never TanStack Query (§21.2). Batched frames carry every
 * changed value at 10–15 fps; routing them through query invalidation triggers
 * dozens of independent re-renders per frame and will not survive a
 * multi-fixture fade on a tablet. useSyncExternalStore is built into React and
 * is exactly the right primitive, so the store below has no dependency.
 *
 * Three rules hold the whole thing up:
 *
 *  1. Per-key subscription. Listeners are registered against the key of the
 *     thing watched. A frame applies all of its changes, collects the touched
 *     keys, then notifies only those listeners — work proportional to what
 *     changed, not to how many components exist.
 *
 *  2. getSnapshot returns primitives. A snapshot that builds a fresh object per
 *     call re-renders forever, because React compares by reference. The few
 *     values that genuinely need an object (colour, a mixer strip, a meter
 *     list) are cached per key and their reference is replaced only when a
 *     component actually changes. Do not "simplify" that caching away.
 *
 *  3. Two maps, one resolution rule. `authoritative` is written only by frames;
 *     `pending` only by local gestures; a snapshot is `pending ?? authoritative`.
 *     Reconciliation is one map delete in both directions — on acknowledgement
 *     the component falls through to a value that is already correct, and on
 *     rejection to the authoritative value, which is current rather than stale.
 *     There is deliberately no restore path: saving the previous value and
 *     putting it back loses every frame that arrived during the gesture.
 */
import { useSyncExternalStore } from "react";

import type { ApiErrorCode } from "@/api/client";

// -- vocabulary ---------------------------------------------------------------------

/** A store key: the thing a listener watches. Namespaced by kind, see the builders. */
export type Key = string;

export type Listener = () => void;

/** §16.1's closed vocabulary; WebSocket nacks draw from the same list (§16.8, B35). */
export type NackReason = ApiErrorCode;

export type ExternalControl = "off" | "detected" | "manual";

/** §7.3, §21.13: which surface last moved a mixer channel. */
export type Origin = "app" | "mixpad" | "surface";

export type SceneOutcome = "ok" | "partial" | "failed";

export interface SceneResult {
  sceneId: number;
  result: SceneOutcome;
}

/**
 * A panel button's lamp (`status` frame, phase-5-contracts.md): `on`
 * is `null` while the derived status has no answer yet, and `transitioning`
 * is the §21.9 amber pulse — a `device_state` status whose device is
 * currently warming or cooling. A button with no `state_id` never reads one
 * (it never latches).
 */
export interface LampState {
  on: boolean | null;
  transitioning: boolean;
}

/** Colour components are 0–255 (§9.2). RGB and RGBW only; `w` is null on RGB. */
export interface Colour {
  r: number;
  g: number;
  b: number;
  w: number | null;
}

export interface MixerStrip {
  /** dB, or null for off — never a wire format and never a position (§5.5, B41). */
  db: number | null;
  muted: boolean;
  origin: Origin;
}

/**
 * Whether the mixer's native metering connection is up, and why not when it
 * is not (§21.9, §21.13, `docs/plans/phase-4-contracts.md`). `reason`
 * is one of the backend's closed codes (kept as `string` here rather than
 * importing `@/mixer`'s narrower union — this module defines its own
 * vocabulary, mirroring `Origin`/`FailureKind` above); `null` exactly when
 * `available` is `true`. `null` for the whole state means "no frame has said
 * anything yet" — the caller falls back to `GET /mixer/state`'s own
 * `metering_reason` for that case, the same fallback `MixerStrip` gets from
 * `GET /mixer/state`'s channel objects until a frame arrives.
 */
export interface MixerMeteringState {
  available: boolean;
  reason: string | null;
}

/**
 * §16.8 splits `mixer_state` into `main`, `outputs` and `inputs`, and an output
 * id may collide with an input id, so a mixer key carries its section. A bare
 * number is an input strip, which is what §21.2's `Map<number, …>` and the
 * `set` frame's integer `id` address.
 */
export type MixerSection = "input" | "output" | "main";

export interface MixerRef {
  section: MixerSection;
  id: number;
}

export type MixerChannel = number | MixerRef;

export type DeviceStatus = "connecting" | "connected" | "degraded" | "error" | "unconfigured";

/** Failure kinds (§5.3, B38): config = connect failed; device = probe failed. */
export type FailureKind = "refused" | "timeout" | "config" | "device";

export interface DeviceDetail {
  name: string;
  status: DeviceStatus;
  last_seen: string | null;
  host: string | null;
  port: number | null;
  protocol: string | null;
  latency_ms: number | null;
  reconnects: number | null;
  last_error: string | null;
  kind: FailureKind | null;
}

export interface DeviceState {
  status: DeviceStatus;
  detail?: DeviceDetail;
}

export const DEVICE_ORDER = ["knx", "dmx", "mixer", "projector", "hdmi"] as const;
export type DeviceName = (typeof DEVICE_ORDER)[number];

/**
 * §21.26's levels, as the server sends them (`proskenion/core/events.py`).
 * "info" is the level; blue is the colour the interface gives it.
 */
export type BannerLevel = "red" | "amber" | "info";

export interface Banner {
  level: BannerLevel;
  key: string;
  text: string;
}

export interface TimerState {
  running: boolean;
  /** Epoch ms when the current run started, null when stopped. */
  startedAt: number | null;
  /** Elapsed ms accumulated from earlier runs since the last reset. */
  accumulated: number;
}

export interface ScenesState {
  running: ReadonlySet<number>;
  lastResult: SceneResult | null;
}

export interface SurfaceState {
  connected: boolean;
  bank: number;
  bankName: string | null;
  touched: ReadonlySet<number>;
}

export interface ProgressState {
  operation: string;
  step: number;
  of: number;
  message: string;
}

export type ConnectionState = "connected" | "reconnecting" | "lost";

/** §21.27: what a resolved write left behind, for the control to show. */
export interface WriteFeedback {
  /** Increments per outcome so a component can re-run its animation. */
  seq: number;
  /** A clamp is a success, not a failure (§16.1) — the value was accepted, just not as sent. */
  kind: "clamped" | "rejected";
  reason: NackReason;
}

// -- keys ---------------------------------------------------------------------------

export const levelKey = (channelId: number): Key => `level:${channelId}`;
export const colourKey = (channelId: number): Key => `colour:${channelId}`;
/**
 * A group fader's own key. It never holds an authoritative value: a group has
 * no stored level of its own (owner decision 2026-09-30 — a group fader sets
 * its members' levels, and `lighting_state` has no `groups` section). The key
 * carries only the pending overlay of a drag in flight, so the fader holds
 * the value under the finger until the write is acknowledged; otherwise the
 * fader shows its members' levels (`useGroupLevel`).
 */
export const groupKey = (groupId: number): Key => `group:${groupId}`;
export const bindingKey = (bankId: number): Key => `binding:${bankId}`;
export const observedKey = (channelId: number): Key => `observed:${channelId}`;
export const meterKey = (channelId: number): Key => `meter:${channelId}`;
/** A button's lamp, keyed by `PanelButtonSpec.state_id` (the `status` domain, phase-5-contracts.md). */
export const lampKey = (stateId: number): Key => `lamp:${stateId}`;
export const deviceKey = (name: string): Key => `device:${name}`;
export const surfaceTouchKey = (strip: number): Key => `surface_touch:${strip}`;
export const progressKey = (operation: string): Key => `progress:${operation}`;
export const feedbackKey = (key: Key): Key => `feedback:${key}`;

/**
 * `pages_changed` (Phase 5 contracts, "Additions"): a page's layout,
 * or a hirer's assignment, changed. The live store only carries the frame —
 * it stays framework-agnostic (§21.2) — a bridge outside it (`App.tsx`'s
 * `LiveConnection`) turns "this key changed" into a TanStack Query
 * invalidation for whichever pages screen is open.
 */
export const PAGES_CHANGED_KEY: Key = "pages_changed";

export const MASTER_KEY: Key = "master";
export const EXTERNAL_CONTROL_KEY: Key = "external_control";
export const SCENES_KEY: Key = "scenes";
export const SURFACE_KEY: Key = "surface";
export const TIMER_KEY: Key = "timer";
export const BANNERS_KEY: Key = "banners";
export const CONNECTION_KEY: Key = "connection";
export const PROJECTOR_KEY: Key = "projector";
export const METERS_AT_KEY: Key = "meters_at";
export const MIXER_METERING_KEY: Key = "mixer_metering";

/** One destination's live routing (§7.5, §16.8) — an output id may repeat across destinations, so this is keyed by destination, not by output. */
export const hdmiDestinationKey = (destinationId: number): Key => `hdmi_destination:${destinationId}`;

const METER_PREFIX = "meter:";
const OBSERVED_PREFIX = "observed:";
const MIXER_PREFIX = "mixer:";
const LAMP_PREFIX = "lamp:";
const PROGRESS_PREFIX = "progress:";

/**
 * Main is a singleton per device and `mixer_state` never keys it by channel id
 * (§16.8: `"main": {"db": 0.0, "muted": false}`, a bare object) — `applyMixer`
 * always stores it at internal id 0. A write target may still carry Main's
 * real `channel_id` (§16.8's `set` example addresses by channel id, and
 * `POST /mixer/channels/{id}/level` does too), so this normalises the
 * *section* is always what selects the bucket for Main, whatever numeric id
 * the caller attaches to the ref — keeping the wire id (real channel_id) and
 * the store key (always 0 for Main) from being conflated.
 */
export function mixerKey(channel: MixerChannel): Key {
  if (typeof channel === "number") return `mixer:input:${channel}`;
  if (channel.section === "main") return "mixer:main:0";
  return `mixer:${channel.section}:${channel.id}`;
}

/** Which key a `set` frame writes (§16.8's settable domains). */
export type WriteDomain = "lighting" | "lighting_group" | "master" | "mixer";

/**
 * A write's target. Lighting and the group/master domains only ever carry a
 * bare channel id; the mixer domain also carries a `MixerRef` when the target
 * is an output or Main, so the pending key lands in the same section-scoped
 * bucket a `mixer_state` frame will later confirm into (§16.8) — without this,
 * a write to an output or Main channel silently fell into the input section's
 * bucket, where no component was reading it.
 */
export type WriteTarget = MixerChannel;

export function writeKey(domain: WriteDomain, id: WriteTarget | null): Key {
  switch (domain) {
    case "lighting":
      return levelKey(typeof id === "number" ? id : 0);
    case "lighting_group":
      return groupKey(typeof id === "number" ? id : 0);
    case "master":
      return MASTER_KEY;
    case "mixer":
      return mixerKey(id ?? 0);
  }
}

// -- the two maps -------------------------------------------------------------------

/** Written only by frames. Never polluted by an unconfirmed value. */
const authoritative = new Map<Key, unknown>();

interface PendingEntry {
  value: unknown;
  /** The write this entry belongs to; null for a gesture that has not been sent. */
  token: number | null;
  acknowledged: boolean;
}

/** Written only by local gestures. */
const pending = new Map<Key, PendingEntry>();

/** token → key, so an ack or nack finds the pending entry it refers to (§16.8). */
const tokens = new Map<number, Key>();

/** Keys with a pointer down on them (§21.2 gesture arbitration). */
const gestures = new Set<Key>();

const feedbacks = new Map<Key, WriteFeedback>();

const listeners = new Map<Key, Set<Listener>>();

let batch: Set<Key> | null = null;
let feedbackSeq = 0;

function notify(key: Key): void {
  const set = listeners.get(key);
  if (!set) return;
  // Copied: a listener may unsubscribe itself while being notified.
  for (const listener of [...set]) listener();
}

/**
 * Mark a key changed. Inside a frame the keys are collected and notified once
 * at the end, so a frame touching two channels wakes two components rather
 * than every subscriber in the application.
 */
function touch(key: Key): void {
  if (batch) batch.add(key);
  else notify(key);
}

/** Apply a set of changes as one frame: all writes first, then one notification per key. */
export function applyFrame<T>(apply: () => T): T {
  if (batch) return apply(); // a nested frame joins the outer one
  const keys = new Set<Key>();
  batch = keys;
  try {
    return apply();
  } finally {
    batch = null;
    for (const key of keys) notify(key);
  }
}

/** The §21.2 resolution rule, and the only way anything reads state. */
function read<T>(key: Key): T | undefined {
  const entry = pending.get(key);
  if (entry !== undefined) return entry.value as T;
  return authoritative.get(key) as T | undefined;
}

/**
 * Write from a frame. A key with a pending entry is written silently: the
 * visible value is the gesture's, so no listener is woken and no fader moves
 * under a finger.
 */
function put(key: Key, value: unknown): void {
  if (Object.is(authoritative.get(key), value)) return;
  authoritative.set(key, value);
  if (!pending.has(key)) touch(key);
}

export function subscribeKey(key: Key, listener: Listener): () => void {
  let set = listeners.get(key);
  if (!set) {
    set = new Set();
    listeners.set(key, set);
  }
  const registered = set;
  registered.add(listener);
  return () => {
    registered.delete(listener);
    if (registered.size === 0) listeners.delete(key);
  };
}

function subscribeKeys(keys: readonly Key[], listener: Listener): () => void {
  const off = keys.map((key) => subscribeKey(key, listener));
  return () => {
    for (const stop of off) stop();
  };
}

/** Diagnostics and tests: how many listeners a key has. */
export function listenerCount(key: Key): number {
  return listeners.get(key)?.size ?? 0;
}

// -- gestures and the pending overlay -------------------------------------------------

/**
 * Record a local gesture value. Never called by a frame, and never for a meter:
 * there is no gesture that produces a meter, and a meter must not reach
 * anything that could act on it (§5.5, B58).
 */
export function setPending(key: Key, value: unknown, token: number | null = null): void {
  if (key.startsWith(METER_PREFIX)) {
    throw new Error(`${key}: meters are display-only and are never written to pending (§5.5, B58)`);
  }
  const previous = pending.get(key);
  if (previous?.token != null) tokens.delete(previous.token); // superseded mid-drag
  pending.set(key, { value, token, acknowledged: false });
  if (token !== null) tokens.set(token, key);
  touch(key);
}

/**
 * A `set` frame carries a bare number (§16.8), but a mixer key holds a strip.
 * This puts the written dB back into the shape the key stores, so the pending
 * overlay and a nack's authoritative value are the same kind of thing as the
 * frames around them. Mute and origin come from the last frame — mute is a
 * discrete action over REST, not part of a continuous write.
 */
function composeWriteValue(key: Key, value: unknown): unknown {
  if (!key.startsWith(MIXER_PREFIX)) return value;
  const previous = authoritative.get(key) as MixerStrip | undefined;
  return Object.freeze({
    db: typeof value === "number" ? value : null,
    muted: previous?.muted ?? false,
    origin: previous?.origin ?? "app",
  });
}

/**
 * The pending entry for one continuous write, in the shape its key holds.
 * `value` is `number | null` for the mixer domain only — `null` is a fader
 * dragged to off (§5.5); `composeWriteValue` already treats anything that is
 * not a `number` as off, so this is simply carrying that possibility through
 * the type. Lighting, the group and master domains never send `null`.
 */
export function setPendingWrite(domain: WriteDomain, id: WriteTarget | null, value: number | null, token: number): Key {
  const key = writeKey(domain, id);
  setPending(key, composeWriteValue(key, value), token);
  return key;
}

export function hasPending(key: Key): boolean {
  return pending.has(key);
}

export function beginGesture(key: Key): void {
  gestures.add(key);
}

/**
 * Pointer up. The pending entry clears once the write it carries has been
 * acknowledged, and the control settles to whatever the server actually holds.
 * If a scene ran during the gesture the control visibly moves here — that is
 * correct and honest; hiding the divergence would be worse (§21.2, §10.6).
 */
export function endGesture(key: Key): void {
  gestures.delete(key);
  const entry = pending.get(key);
  if (!entry) return;
  if (entry.token === null || entry.acknowledged) {
    if (entry.token !== null) tokens.delete(entry.token);
    pending.delete(key);
    touch(key);
  }
}

export function isGesturing(key: Key): boolean {
  return gestures.has(key);
}

/** Acknowledged: delete the pending entry. No second write, no flash. */
export function resolveAck(token: number): Key | null {
  const key = tokens.get(token);
  if (key === undefined) return null;
  tokens.delete(token);
  const entry = pending.get(key);
  if (!entry || entry.token !== token) return null;
  if (gestures.has(key)) {
    // The pointer is still down: hold the gesture value until release, or the
    // control jumps under the finger to a frame that is one drag behind.
    entry.acknowledged = true;
    return key;
  }
  pending.delete(key);
  touch(key);
  return key;
}

/**
 * Rejected: delete the pending entry and let the component fall through to the
 * authoritative value, which is current rather than stale. Where the nack
 * carries the authoritative value (a hirer write clamped to a ceiling, §16.8)
 * that value is written first, so the control settles at the right place.
 */
export function resolveNack(token: number, reason: NackReason, value?: unknown): Key | null {
  const key = tokens.get(token);
  if (key === undefined) return null;
  tokens.delete(token);
  const entry = pending.get(key);
  if (!entry || entry.token !== token) return null;
  applyFrame(() => {
    if (value !== undefined) authoritative.set(key, composeWriteValue(key, value));
    pending.delete(key);
    gestures.delete(key);
    touch(key);
    // value_out_of_range is a success with a clamp, not a failure (§16.1).
    feedbacks.set(key, {
      seq: ++feedbackSeq,
      kind: reason === "value_out_of_range" ? "clamped" : "rejected",
      reason,
    });
    touch(feedbackKey(key));
  });
  return key;
}

/**
 * Reconnect: drop every write in flight. Nothing is replayed (§10.7) —
 * replaying a queued mute-off after a 90-second outage could unmute a live
 * microphone on an intent formed a minute and a half earlier, in a room whose
 * state has since moved on.
 */
export function abandonWrites(): void {
  applyFrame(() => {
    for (const [key, entry] of [...pending]) {
      if (entry.token === null) continue;
      pending.delete(key);
      touch(key);
    }
    tokens.clear();
    gestures.clear();
  });
}

export function getFeedback(key: Key): WriteFeedback | null {
  return feedbacks.get(key) ?? null;
}

export function clearFeedback(key: Key): void {
  if (feedbacks.delete(key)) touch(feedbackKey(key));
}

// -- snapshots ----------------------------------------------------------------------
//
// Every getter below returns a primitive, or an object whose reference is
// replaced only when its contents change. Neither builds anything per call.

export function getLevel(channelId: number): number | null {
  return read<number>(levelKey(channelId)) ?? null;
}

export function getColour(channelId: number): Colour | null {
  return read<Colour>(colourKey(channelId)) ?? null;
}

/** A group fader's drag in flight (0–100), or `null` — see `groupKey`. */
export function getGroup(groupId: number): number | null {
  return read<number>(groupKey(groupId)) ?? null;
}

export function getMaster(): number | null {
  return read<number>(MASTER_KEY) ?? null;
}

export function getBinding(bankId: number): boolean {
  return read<boolean>(bindingKey(bankId)) ?? false;
}

export function getObserved(channelId: number): number | null {
  return read<number>(observedKey(channelId)) ?? null;
}

export function getExternalControl(): ExternalControl {
  return read<ExternalControl>(EXTERNAL_CONTROL_KEY) ?? "off";
}

/**
 * The display rule of §7.2.7: while external control is active the observed
 * level is shown in preference to the controller's own model. `observed` and
 * `levels` stay separate maps and are never merged — this reads whichever the
 * rule selects, and the indicator that says so is the component's job.
 */
export function getDisplayLevel(channelId: number): number | null {
  if (getExternalControl() !== "off") {
    const observed = getObserved(channelId);
    if (observed !== null) return observed;
  }
  return getLevel(channelId);
}

export function getMixer(channel: MixerChannel): MixerStrip | null {
  return read<MixerStrip>(mixerKey(channel)) ?? null;
}

export function getMixerDb(channel: MixerChannel): number | null {
  return getMixer(channel)?.db ?? null;
}

export function getMixerMuted(channel: MixerChannel): boolean {
  return getMixer(channel)?.muted ?? false;
}

/**
 * One entry per driver reference in sort order, index 0 = left (§5.5). `null`
 * for the whole channel means absent — no data at all — which is what lets the
 * interface render no bar rather than a bar at the floor (B58). Read straight
 * from `authoritative`: a meter is never pending.
 */
export function getMeter(channelId: number): readonly (number | null)[] | null {
  return (authoritative.get(meterKey(channelId)) as readonly (number | null)[] | undefined) ?? null;
}

/** Monotonic timestamp of the last meter frame, for staleness (§5.5). */
export function getMeterAt(): number | null {
  return (authoritative.get(METERS_AT_KEY) as number | undefined) ?? null;
}

/**
 * A button's lamp, by `state_id`. `null` means no `status` frame has said
 * anything about this state yet — the button renders unlit, same as `on:
 * false`, until one arrives.
 */
export function getLamp(stateId: number): LampState | null {
  return read<LampState>(lampKey(stateId)) ?? null;
}

export function setLamp(stateId: number, state: LampState): void {
  const key = lampKey(stateId);
  const previous = authoritative.get(key) as LampState | undefined;
  if (previous && previous.on === state.on && previous.transitioning === state.transitioning) return;
  put(key, Object.freeze({ ...state }));
}

/**
 * Resync (§16.8): "a resync or a newly opened socket sends every lamp the
 * connection may see" — which lamp *that* is can shrink (a page reassigned,
 * a hirer's access narrowed), so stale lamps are dropped first, exactly as
 * `clearMeters` drops stale meters before the fresh frames land.
 */
export function clearLamps(): void {
  applyFrame(() => {
    for (const key of [...authoritative.keys()]) {
      if (!key.startsWith(LAMP_PREFIX)) continue;
      authoritative.delete(key);
      touch(key);
    }
  });
}

/**
 * `null` until a `mixer_meters` frame has carried `metering` this session —
 * see `MixerMeteringState`'s own doc comment for what a caller does with
 * that.
 */
export function getMixerMetering(): MixerMeteringState | null {
  return read<MixerMeteringState>(MIXER_METERING_KEY) ?? null;
}

const UNCONFIGURED: DeviceState = Object.freeze({ status: "unconfigured" });

export function getDeviceState(name: DeviceName): DeviceState {
  return read<DeviceState>(deviceKey(name)) ?? UNCONFIGURED;
}

export function getDeviceStatus(name: DeviceName): DeviceStatus {
  return getDeviceState(name).status;
}

const NO_SCENES: ScenesState = Object.freeze({ running: new Set<number>(), lastResult: null });

export function getScenes(): ScenesState {
  return read<ScenesState>(SCENES_KEY) ?? NO_SCENES;
}

export function isSceneRunning(sceneId: number): boolean {
  return getScenes().running.has(sceneId);
}

const NO_SURFACE: SurfaceState = Object.freeze({
  connected: false,
  bank: 0,
  bankName: null,
  touched: new Set<number>(),
});

export function getSurface(): SurfaceState {
  return read<SurfaceState>(SURFACE_KEY) ?? NO_SURFACE;
}

export function isStripTouched(strip: number): boolean {
  return read<boolean>(surfaceTouchKey(strip)) ?? false;
}

const STOPPED_TIMER: TimerState = Object.freeze({ running: false, startedAt: null, accumulated: 0 });

export function getTimerState(): TimerState {
  return read<TimerState>(TIMER_KEY) ?? STOPPED_TIMER;
}

const NO_BANNERS: readonly Banner[] = Object.freeze([]);

/** Sorted by priority, red over amber over info (§21.26). */
export function getBanners(): readonly Banner[] {
  return read<readonly Banner[]>(BANNERS_KEY) ?? NO_BANNERS;
}

export function getProgress(operation: string): ProgressState | null {
  return read<ProgressState>(progressKey(operation)) ?? null;
}

export function getConnectionState(): ConnectionState {
  return read<ConnectionState>(CONNECTION_KEY) ?? "connected";
}

/**
 * During an outage the existing state stays on screen and every control is
 * disabled, so no write is issued that would be lost (§10.7).
 */
export function controlsEnabled(): boolean {
  return getConnectionState() === "connected";
}

/**
 * The projector's power state and current input (the `projector_state`
 * frame, phase-3-contracts.md). `remaining_s` and the `inputs` list are
 * configuration-shaped and travel with the REST response instead (§16.5) —
 * the frame carries only what changes on every state or input transition.
 */
export interface ProjectorLiveState {
  state: string;
  inputRef: string | null;
}

export function getProjectorState(): ProjectorLiveState | null {
  return read<ProjectorLiveState>(PROJECTOR_KEY) ?? null;
}

export function setProjectorState(state: ProjectorLiveState): void {
  const previous = read<ProjectorLiveState>(PROJECTOR_KEY);
  if (previous && previous.state === state.state && previous.inputRef === state.inputRef) return;
  put(PROJECTOR_KEY, Object.freeze({ ...state }));
}

/**
 * One destination's routed input and whether its outputs currently disagree
 * (§7.5 divergence). Seeded from `GET /hdmi/state` and kept current by the
 * `hdmi_source` frame, which fires on every routing change whether it came
 * from the application or from the front panel.
 */
export interface HdmiDestinationLiveState {
  inputId: number | null;
  diverged: boolean;
}

export function getHdmiDestinationState(destinationId: number): HdmiDestinationLiveState | null {
  return read<HdmiDestinationLiveState>(hdmiDestinationKey(destinationId)) ?? null;
}

/**
 * Write an authoritative routing value — from `GET /hdmi/state`'s snapshot,
 * a confirmed `POST`, or a frame. Any of the three is "the response or the
 * frame" of §21.14: whichever arrives first resolves a press, so this always
 * clears a pending overlay on the same key, even when the resolved value is
 * not what was pressed — someone else's change is still the truth.
 */
export function setHdmiDestinationState(destinationId: number, state: HdmiDestinationLiveState): void {
  const key = hdmiDestinationKey(destinationId);
  applyFrame(() => {
    const previous = authoritative.get(key) as HdmiDestinationLiveState | undefined;
    const authoritativeChanged = !previous || previous.inputId !== state.inputId || previous.diverged !== state.diverged;
    if (authoritativeChanged) authoritative.set(key, Object.freeze({ ...state }));
    const hadPending = pending.delete(key);
    if (authoritativeChanged || hadPending) touch(key);
  });
}

/**
 * Clear an optimistic overlay set without a token — a REST press this store
 * has no ack/nack for. Used when the press is rejected: nothing is written
 * to `authoritative`, so the control simply falls back to whatever is
 * already confirmed there (§16.1, §21.27).
 */
export function clearPending(key: Key): void {
  if (pending.delete(key)) touch(key);
}

// -- hooks --------------------------------------------------------------------------

function useKey<T>(key: Key, snapshot: () => T): T {
  return useSyncExternalStore((listener) => subscribeKey(key, listener), snapshot, snapshot);
}

function useAnyKey<T>(keys: readonly Key[], snapshot: () => T): T {
  return useSyncExternalStore((listener) => subscribeKeys(keys, listener), snapshot, snapshot);
}

export const subscribeLevel = (channelId: number, cb: Listener) => subscribeKey(levelKey(channelId), cb);
export const subscribeColour = (channelId: number, cb: Listener) => subscribeKey(colourKey(channelId), cb);
export const subscribeGroup = (groupId: number, cb: Listener) => subscribeKey(groupKey(groupId), cb);
export const subscribeBinding = (bankId: number, cb: Listener) => subscribeKey(bindingKey(bankId), cb);
export const subscribeObserved = (channelId: number, cb: Listener) => subscribeKey(observedKey(channelId), cb);
export const subscribeMixer = (channel: MixerChannel, cb: Listener) => subscribeKey(mixerKey(channel), cb);
export const subscribeMeter = (channelId: number, cb: Listener) => subscribeKey(meterKey(channelId), cb);
export const subscribeLamp = (stateId: number, cb: Listener) => subscribeKey(lampKey(stateId), cb);
export const subscribeDevice = (name: DeviceName, cb: Listener) => subscribeKey(deviceKey(name), cb);
export const subscribeMaster = (cb: Listener) => subscribeKey(MASTER_KEY, cb);
export const subscribeExternalControl = (cb: Listener) => subscribeKey(EXTERNAL_CONTROL_KEY, cb);
export const subscribeScenes = (cb: Listener) => subscribeKey(SCENES_KEY, cb);
export const subscribeSurface = (cb: Listener) => subscribeKey(SURFACE_KEY, cb);
export const subscribeTimer = (cb: Listener) => subscribeKey(TIMER_KEY, cb);
export const subscribeBanners = (cb: Listener) => subscribeKey(BANNERS_KEY, cb);
export const subscribeProgress = (operation: string, cb: Listener) => subscribeKey(progressKey(operation), cb);
export const subscribeConnection = (cb: Listener) => subscribeKey(CONNECTION_KEY, cb);
export const subscribeFeedback = (key: Key, cb: Listener) => subscribeKey(feedbackKey(key), cb);

export const useLevel = (channelId: number): number | null => useKey(levelKey(channelId), () => getLevel(channelId));
export const useColour = (channelId: number): Colour | null => useKey(colourKey(channelId), () => getColour(channelId));
export const useGroup = (groupId: number): number | null => useKey(groupKey(groupId), () => getGroup(groupId));
export const useBinding = (bankId: number): boolean => useKey(bindingKey(bankId), () => getBinding(bankId));
export const useObserved = (channelId: number): number | null =>
  useKey(observedKey(channelId), () => getObserved(channelId));
export const useMaster = (): number | null => useKey(MASTER_KEY, getMaster);
export const useExternalControl = (): ExternalControl => useKey(EXTERNAL_CONTROL_KEY, getExternalControl);
export const useMixer = (channel: MixerChannel): MixerStrip | null =>
  useKey(mixerKey(channel), () => getMixer(channel));
export const useMeter = (channelId: number): readonly (number | null)[] | null =>
  useKey(meterKey(channelId), () => getMeter(channelId));
export const useLamp = (stateId: number): LampState | null => useKey(lampKey(stateId), () => getLamp(stateId));
export const useMixerMetering = (): MixerMeteringState | null => useKey(MIXER_METERING_KEY, getMixerMetering);
export const useDeviceState = (name: DeviceName): DeviceState => useKey(deviceKey(name), () => getDeviceState(name));
export const useScenes = (): ScenesState => useKey(SCENES_KEY, getScenes);
export const useSurface = (): SurfaceState => useKey(SURFACE_KEY, getSurface);
export const useStripTouched = (strip: number): boolean => useKey(surfaceTouchKey(strip), () => isStripTouched(strip));
export const useTimerState = (): TimerState => useKey(TIMER_KEY, getTimerState);
export const useBanners = (): readonly Banner[] => useKey(BANNERS_KEY, getBanners);
export const useProgress = (operation: string): ProgressState | null =>
  useKey(progressKey(operation), () => getProgress(operation));
export const useLiveConnection = (): ConnectionState => useKey(CONNECTION_KEY, getConnectionState);
export const useControlsEnabled = (): boolean => useKey(CONNECTION_KEY, controlsEnabled);
export const useFeedback = (key: Key): WriteFeedback | null => useKey(feedbackKey(key), () => getFeedback(key));
export const subscribeProjectorState = (cb: Listener) => subscribeKey(PROJECTOR_KEY, cb);
export const useProjectorState = (): ProjectorLiveState | null => useKey(PROJECTOR_KEY, getProjectorState);
export const subscribeHdmiDestination = (destinationId: number, cb: Listener) =>
  subscribeKey(hdmiDestinationKey(destinationId), cb);
export const useHdmiDestinationState = (destinationId: number): HdmiDestinationLiveState | null =>
  useKey(hdmiDestinationKey(destinationId), () => getHdmiDestinationState(destinationId));

/** The §7.2.7 display rule needs three keys; it still returns a primitive. */
export const useDisplayLevel = (channelId: number): number | null =>
  useAnyKey([levelKey(channelId), observedKey(channelId), EXTERNAL_CONTROL_KEY], () => getDisplayLevel(channelId));

// -- writers ------------------------------------------------------------------------

export function setLevel(channelId: number, level: number): void {
  put(levelKey(channelId), level);
}

/**
 * Colour is the one live value that genuinely needs an object. The reference is
 * replaced only when a component actually changes, so equality still holds
 * between frames and a channel whose colour is steady does not re-render while
 * its level fades. Do not replace this with a fresh object per frame.
 */
export function setColour(channelId: number, components: Partial<Colour>): void {
  const key = colourKey(channelId);
  const previous = authoritative.get(key) as Colour | undefined;
  const next: Colour = {
    r: components.r ?? previous?.r ?? 0,
    g: components.g ?? previous?.g ?? 0,
    b: components.b ?? previous?.b ?? 0,
    w: components.w ?? previous?.w ?? null,
  };
  if (previous && previous.r === next.r && previous.g === next.g && previous.b === next.b && previous.w === next.w) {
    return;
  }
  put(key, Object.freeze(next));
}

export function setMaster(level: number): void {
  put(MASTER_KEY, level);
}

export function setBinding(bankId: number, active: boolean): void {
  put(bindingKey(bankId), active);
}

export function setObserved(channelId: number, level: number | null): void {
  const key = observedKey(channelId);
  if (level === null) {
    if (!authoritative.has(key)) return;
    authoritative.delete(key);
    if (!pending.has(key)) touch(key);
    return;
  }
  put(key, level);
}

export function setExternalControl(state: ExternalControl): void {
  put(EXTERNAL_CONTROL_KEY, state);
}

/** The most recent `pages_changed` frame's `page_ids` — never replayed on resync. */
export function getPagesChangedIds(): readonly number[] {
  return read<readonly number[]>(PAGES_CHANGED_KEY) ?? [];
}

export function setPagesChangedIds(ids: readonly number[]): void {
  // A new array every time, even with identical ids: two separate edits can
  // land on the same page set, and the bridge to TanStack Query should
  // still refetch for the second one.
  put(PAGES_CHANGED_KEY, ids);
}

export function setMixer(channel: MixerChannel, strip: Partial<MixerStrip>): void {
  const key = mixerKey(channel);
  const previous = authoritative.get(key) as MixerStrip | undefined;
  const next: MixerStrip = {
    db: strip.db !== undefined ? strip.db : (previous?.db ?? null),
    muted: strip.muted ?? previous?.muted ?? false,
    origin: strip.origin ?? previous?.origin ?? "app",
  };
  if (previous && previous.db === next.db && previous.muted === next.muted && previous.origin === next.origin) {
    return;
  }
  put(key, Object.freeze(next));
}

/** Meters live beside the mixer state, never inside it (§21.2 store shape). */
export function setMeter(channelId: number, values: readonly (number | null)[]): void {
  put(meterKey(channelId), Object.freeze([...values]));
}

/**
 * Written from a `mixer_meters` frame's `metering`: `available:
 * false` is what the mixer view clears every bar and shows the notice for;
 * `available: true` is what hides it again. See `MixerMeteringState`.
 */
export function setMixerMetering(state: MixerMeteringState): void {
  put(MIXER_METERING_KEY, Object.freeze({ ...state }));
}

/**
 * Cleared first, so a channel whose reading turns out to be unavailable
 * shows nothing rather than a frozen value from before the drop — but never
 * left empty for long: the server answers every `resync` with a fresh
 * `mixer_meters` catch-up naming every channel's *current* reading, read
 * live rather than replayed (§16.8, B58), so bars reappear as that frame
 * arrives rather than waiting on the next change. The metering-availability
 * marker is cleared with it, for the same reason, and is carried by that
 * same catch-up frame; a client falls back to `GET /mixer/state`'s own
 * `metering_reason` only until it arrives.
 */
export function clearMeters(): void {
  applyFrame(() => {
    for (const key of [...authoritative.keys()]) {
      if (!key.startsWith(METER_PREFIX)) continue;
      authoritative.delete(key);
      touch(key);
    }
    authoritative.delete(METERS_AT_KEY);
    if (authoritative.delete(MIXER_METERING_KEY)) touch(MIXER_METERING_KEY);
  });
}

/**
 * Never replayed on resync either: `progress` frames are discrete
 * "this step just started" events with no resync-carried snapshot behind
 * them (unlike `scenes_state`), so a client that misses the final frame — a
 * socket dropping mid-operation and reconnecting after the job actually
 * finished — would otherwise be left showing `step < of` forever, with
 * nothing left to ever complete it. Dropping it here, on the same resync
 * that already clears meters and lamps, means a still-running operation is
 * simply re-established by its own next frame, while a finished one falls
 * back to whatever the initiating request already resolved, or the next
 * status query returns — never stuck.
 */
export function clearProgress(): void {
  applyFrame(() => {
    for (const key of [...authoritative.keys()]) {
      if (!key.startsWith(PROGRESS_PREFIX)) continue;
      authoritative.delete(key);
      touch(key);
    }
  });
}

/**
 * Clears one operation's progress the moment the request that started it has
 * its own answer — a caller that already knows, authoritatively, that the
 * operation is over, rather than waiting for a resync or a next frame that
 * this exact race can also miss: `HelperClient.wait()` (`core/helper.py`)
 * relays the terminal status it reads, but that relay travels the WebSocket
 * while the mutation's own answer travels its HTTP response, on separate
 * connections with no ordering between them — so even a fixed backend can
 * have the query settle in the browser before the frame does. A progress
 * panel gating "still running" on `step < of` must not out-live the request
 * whose progress it was showing (backup progress panel stuck on "Running
 * the backup job" after `POST /system/backup/run` had already answered,
 * 25 Sep 2026, v0.1.2). Scoped to one operation, unlike `clearProgress()`'s
 * every-operation sweep on resync, so resolving a `backup_run` never
 * disturbs a `cert_issue` another tab is watching.
 */
export function clearProgressFor(operation: string): void {
  applyFrame(() => {
    const key = progressKey(operation);
    if (authoritative.delete(key)) touch(key);
  });
}

export function setDeviceState(name: DeviceName | string, state: DeviceState): void {
  const next: DeviceState = state.detail ? { status: state.status, detail: state.detail } : { status: state.status };
  put(deviceKey(name), Object.freeze(next));
}

export function setSceneRunning(sceneId: number, running: boolean, result?: SceneResult | null): void {
  const current = getScenes();
  const next = new Set(current.running);
  if (running) next.add(sceneId);
  else next.delete(sceneId);
  put(
    SCENES_KEY,
    Object.freeze({
      running: next,
      lastResult: result !== undefined ? result : current.lastResult,
    }),
  );
}

export function setSurface(patch: Partial<SurfaceState>): void {
  const current = getSurface();
  put(
    SURFACE_KEY,
    Object.freeze({
      connected: patch.connected ?? current.connected,
      bank: patch.bank ?? current.bank,
      bankName: patch.bankName !== undefined ? patch.bankName : current.bankName,
      touched: patch.touched ?? current.touched,
    }),
  );
}

export function setStripTouched(strip: number, touched: boolean): void {
  applyFrame(() => {
    put(surfaceTouchKey(strip), touched);
    const current = getSurface();
    const next = new Set(current.touched);
    if (touched) next.add(strip);
    else next.delete(strip);
    setSurface({ touched: next });
  });
}

export function setTimerState(state: TimerState): void {
  const current = getTimerState();
  if (
    current.running === state.running &&
    current.startedAt === state.startedAt &&
    current.accumulated === state.accumulated
  ) {
    return;
  }
  put(TIMER_KEY, Object.freeze({ ...state }));
}

const BANNER_PRIORITY: Readonly<Record<BannerLevel, number>> = { red: 0, amber: 1, info: 2 };

/** A cleared banner is `"text": null` (§16.8), not an absent message. */
export function setBanner(key: string, level: BannerLevel, text: string | null): void {
  const current = getBanners();
  const without = current.filter((banner) => banner.key !== key);
  const next = text === null ? without : [...without, { key, level, text }];
  next.sort((a, b) => BANNER_PRIORITY[a.level] - BANNER_PRIORITY[b.level] || a.key.localeCompare(b.key));
  const unchanged =
    next.length === current.length &&
    next.every((banner, index) => {
      const before = current[index];
      return before?.key === banner.key && before.level === banner.level && before.text === banner.text;
    });
  if (unchanged) return;
  put(BANNERS_KEY, Object.freeze(next));
}

export function setProgress(state: ProgressState): void {
  put(progressKey(state.operation), Object.freeze({ ...state }));
}

export function setConnectionState(state: ConnectionState): void {
  put(CONNECTION_KEY, state);
}

// -- frame application ---------------------------------------------------------------

type Frame = Record<string, unknown>;

function asNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function asMap(value: unknown): Frame {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Frame) : {};
}

function channelId(key: string): number | null {
  if (key === "") return null;
  const id = Number(key);
  return Number.isInteger(id) ? id : null;
}

/**
 * Epoch ms from §4.9's ISO 8601 with offset. §16.8's example prints a float
 * instead; the server sends the ISO form, and a number is read as epoch
 * seconds so either shape lands in the same place.
 */
export function parseInstant(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return Math.round(value * 1000);
  if (typeof value !== "string") return null;
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? null : parsed;
}

function applyLighting(frame: Frame): void {
  for (const [raw, entry] of Object.entries(asMap(frame["channels"]))) {
    const id = channelId(raw);
    if (id === null) continue;
    const values = asMap(entry);
    const level = asNumber(values["level"]);
    if (level !== null) setLevel(id, level);
    const colour: Partial<Colour> = {};
    const r = asNumber(values["r"]);
    const g = asNumber(values["g"]);
    const b = asNumber(values["b"]);
    const w = asNumber(values["w"]);
    if (r !== null) colour.r = r;
    if (g !== null) colour.g = g;
    if (b !== null) colour.b = b;
    if (w !== null) colour.w = w;
    if (Object.keys(colour).length > 0) setColour(id, colour);
  }
  // No `groups` section: a group fader sets its members' levels and has no
  // value of its own (owner decision 2026-09-30). An older server's section
  // is ignored — its multipliers no longer mean anything here.
  // Stage-bank states (§7.1), keyed by rule id — for the stage banks
  // row; the frame shape was already documented in LiveState but not read.
  for (const [raw, value] of Object.entries(asMap(frame["bindings"]))) {
    const id = channelId(raw);
    if (id !== null && typeof value === "boolean") setBinding(id, value);
  }
  const master = asNumber(frame["master"]);
  if (master !== null) setMaster(master);
  const external = frame["external_control"];
  if (external === "off" || external === "detected" || external === "manual") setExternalControl(external);
  if ("observed" in frame) {
    // The frame carries the whole observed map, or null for nothing observed —
    // absent rather than at the floor, so the interface falls back to the
    // controller's own model (§7.2.7).
    const observed = frame["observed"] === null ? {} : asMap(frame["observed"]);
    const seen = new Set<number>();
    for (const [raw, value] of Object.entries(observed)) {
      const id = channelId(raw);
      const level = asNumber(value);
      if (id === null || level === null) continue;
      seen.add(id);
      setObserved(id, level);
    }
    for (const key of [...authoritative.keys()]) {
      if (!key.startsWith(OBSERVED_PREFIX)) continue;
      const id = channelId(key.slice(OBSERVED_PREFIX.length));
      if (id !== null && !seen.has(id)) setObserved(id, null);
    }
  }
}

function applyMixerSection(section: MixerSection, values: Frame): void {
  for (const [raw, entry] of Object.entries(values)) {
    const id = section === "main" ? 0 : channelId(raw);
    if (id === null) continue;
    const strip = asMap(entry);
    const patch: Partial<MixerStrip> = {};
    if ("db" in strip) patch.db = asNumber(strip["db"]);
    if (typeof strip["muted"] === "boolean") patch.muted = strip["muted"];
    // Each entry is the channel's whole state, and §16.8 carries `origin`
    // only when it is "mixpad" or "surface" (phase-4-contracts.md): an entry
    // without one is this application's own change or a sync, so it clears
    // the badge rather than leaving the last one standing.
    const origin = strip["origin"];
    patch.origin = origin === "mixpad" || origin === "surface" ? origin : "app";
    setMixer({ section, id }, patch);
  }
}

function applyMixer(frame: Frame): void {
  const main = frame["main"];
  if (main && typeof main === "object") applyMixerSection("main", { "0": main });
  applyMixerSection("output", asMap(frame["outputs"]));
  applyMixerSection("input", asMap(frame["inputs"]));
}

/**
 * `scenes_state`: which scenes the engine reports running
 * right now, and the most recent completed run. This is what lets a tablet
 * that opens its socket mid-scene show the card executing immediately,
 * rather than waiting for the next `scene_started`/`scene_completed` — those
 * two discrete messages still carry "this just happened" and are applied by
 * `setSceneRunning` exactly as before; this frame only fills the gap a
 * resync previously left empty. A partial frame carries only the section
 * that changed, matching `lighting_state`/`mixer_state`.
 */
function applyScenes(frame: Frame): void {
  const current = getScenes();
  let running = current.running;
  if ("running" in frame) {
    const next = new Set<number>();
    for (const raw of Object.keys(asMap(frame["running"]))) {
      const id = channelId(raw);
      if (id !== null) next.add(id);
    }
    running = next;
  }
  let lastResult = current.lastResult;
  if ("last_result" in frame) {
    const raw = frame["last_result"];
    if (raw && typeof raw === "object") {
      const values = raw as Frame;
      const sceneId = asNumber(values["scene_id"]);
      const result = values["result"];
      lastResult =
        sceneId === null
          ? null
          : { sceneId, result: result === "partial" || result === "failed" ? result : "ok" };
    } else {
      lastResult = null;
    }
  }
  put(SCENES_KEY, Object.freeze({ running, lastResult }));
}

/**
 * `mixer_meters` (§16.8). `metering`, present only on an
 * availability change, is applied first: on loss every bar clears at once
 * (`clearMeters` — §21.9, B58: "where metering is unavailable, show
 * nothing") rather than freezing on whatever `channels` last held, and the
 * frame's own `channels` (empty on loss, per the backend contract) is then
 * applied as usual, same as a frame with no `metering` at all.
 */
function applyMeters(frame: Frame): void {
  const metering = frame["metering"];
  if (metering && typeof metering === "object") {
    const values = metering as Frame;
    const available = values["available"] === true;
    const reason = typeof values["reason"] === "string" ? values["reason"] : null;
    if (!available) clearMeters();
    setMixerMetering({ available, reason: available ? null : reason });
  }
  for (const [raw, values] of Object.entries(asMap(frame["channels"]))) {
    const id = channelId(raw);
    if (id === null || !Array.isArray(values)) continue;
    setMeter(
      id,
      (values as unknown[]).map((value) => asNumber(value)),
    );
  }
  const at = asNumber(frame["at"]);
  if (at !== null) put(METERS_AT_KEY, at);
}

/**
 * `status` (phase-5-contracts.md): `{"lamps": {"<state_id>": {"on":
 * bool | null, "transitioning": bool}}}`, discrete and sent on change — a
 * button's lamp is derived status, the same predicate that drives a KNX
 * panel indicator (§21.9), never written by whatever fired the rule (§8.6,
 * B51).
 */
function applyStatus(frame: Frame): void {
  for (const [raw, entry] of Object.entries(asMap(frame["lamps"]))) {
    const stateId = channelId(raw);
    if (stateId === null) continue;
    const values = asMap(entry);
    const on = values["on"];
    setLamp(stateId, {
      on: typeof on === "boolean" ? on : null,
      transitioning: values["transitioning"] === true,
    });
  }
}

/**
 * One server message (§16.8). `ack` and `nack` are not handled here — they are
 * resolved against the token index by the socket, which owns the write.
 */
export function applyMessage(message: unknown): void {
  if (!message || typeof message !== "object") return;
  const frame = message as Frame;
  applyFrame(() => {
    switch (frame["type"]) {
      case "lighting_state":
        applyLighting(frame);
        break;
      case "mixer_state":
        applyMixer(frame);
        break;
      case "mixer_meters":
        applyMeters(frame);
        break;
      case "scenes_state":
        applyScenes(frame);
        break;
      case "status":
        applyStatus(frame);
        break;
      case "pages_changed": {
        const ids = frame["page_ids"];
        if (Array.isArray(ids)) {
          setPagesChangedIds(ids.filter((id): id is number => typeof id === "number"));
        }
        break;
      }
      case "device_status": {
        const device = frame["device"];
        const status = frame["status"];
        if (typeof device === "string" && typeof status === "string") {
          setDeviceState(device, { status: status as DeviceStatus });
        }
        break;
      }
      case "scene_started": {
        const id = asNumber(frame["scene_id"]);
        if (id !== null) setSceneRunning(id, true);
        break;
      }
      case "scene_completed": {
        const id = asNumber(frame["scene_id"]);
        const result = frame["result"];
        if (id !== null) {
          setSceneRunning(id, false, {
            sceneId: id,
            result: result === "partial" || result === "failed" ? result : "ok",
          });
        }
        break;
      }
      case "projector_state": {
        // phase-3-contracts.md: {"type": "projector_state", "state": ..., "input_ref": ...}.
        const state = frame["state"];
        if (typeof state === "string") {
          const inputRef = frame["input_ref"];
          setProjectorState({ state, inputRef: typeof inputRef === "string" ? inputRef : null });
        }
        break;
      }
      case "hdmi_source": {
        // phase-3-contracts.md: {"type": "hdmi_source", "destination_id", "input_id", "diverged"},
        // sent per destination on every routing change (§16.8, as corrected).
        const destinationId = asNumber(frame["destination_id"]);
        if (destinationId !== null) {
          setHdmiDestinationState(destinationId, {
            inputId: asNumber(frame["input_id"]),
            diverged: frame["diverged"] === true,
          });
        }
        break;
      }
      case "external_control": {
        const state = frame["state"];
        if (state === "off" || state === "detected" || state === "manual") setExternalControl(state);
        break;
      }
      case "surface_bank": {
        const bank = asNumber(frame["bank_id"]);
        const name = frame["name"];
        setSurface({
          connected: true,
          ...(bank !== null ? { bank } : {}),
          ...(typeof name === "string" ? { bankName: name } : {}),
        });
        break;
      }
      case "surface_touch": {
        const strip = asNumber(frame["strip"]);
        if (strip !== null) setStripTouched(strip, frame["touched"] === true);
        break;
      }
      case "banner": {
        const key = frame["key"];
        const level = frame["level"];
        const text = frame["text"];
        if (typeof key === "string" && (level === "red" || level === "amber" || level === "info")) {
          setBanner(key, level, typeof text === "string" ? text : null);
        }
        break;
      }
      case "timer": {
        // started_at and accumulated_ms, never a running elapsed count: the
        // client computes elapsed against the appliance clock (§21.7).
        setTimerState({
          running: frame["running"] === true,
          startedAt: parseInstant(frame["started_at"]),
          accumulated: asNumber(frame["accumulated_ms"]) ?? 0,
        });
        break;
      }
      case "progress": {
        const operation = frame["operation"];
        const step = asNumber(frame["step"]);
        const of = asNumber(frame["of"]);
        const text = frame["message"];
        if (typeof operation === "string" && step !== null && of !== null) {
          setProgress({ operation, step, of, message: typeof text === "string" ? text : "" });
        }
        break;
      }
      default:
        break;
    }
  });
}

// -- the whole shape, for tests and diagnostics ---------------------------------------

/** §21.2's LiveState. Assembled on demand — never a getSnapshot, it allocates. */
export interface LiveState {
  lighting: {
    levels: Map<number, number>;
    colours: Map<number, Colour>;
    master: number | null;
    bindings: Map<number, boolean>;
    observed: Map<number, number>;
    externalControl: ExternalControl;
  };
  /** Keyed by `mixerKey`, because an output id may collide with an input id (§16.8). */
  mixer: Map<Key, MixerStrip>;
  meters: Map<number, readonly (number | null)[]>;
  devices: Map<string, DeviceState>;
  scenes: ScenesState;
  surface: SurfaceState;
  timer: TimerState;
  banners: readonly Banner[];
  connection: ConnectionState;
}

export function getLiveState(): LiveState {
  const levels = new Map<number, number>();
  const colours = new Map<number, Colour>();
  const bindings = new Map<number, boolean>();
  const observed = new Map<number, number>();
  const mixer = new Map<Key, MixerStrip>();
  const meters = new Map<number, readonly (number | null)[]>();
  const devices = new Map<string, DeviceState>();
  for (const key of authoritative.keys()) {
    const separator = key.indexOf(":");
    if (separator < 0) continue;
    const kind = key.slice(0, separator);
    const tail = key.slice(separator + 1);
    const id = channelId(tail);
    switch (kind) {
      case "level":
        if (id !== null) levels.set(id, read<number>(key) as number);
        break;
      case "colour":
        if (id !== null) colours.set(id, read<Colour>(key) as Colour);
        break;
      case "binding":
        if (id !== null) bindings.set(id, read<boolean>(key) as boolean);
        break;
      case "observed":
        if (id !== null) observed.set(id, read<number>(key) as number);
        break;
      case "mixer":
        mixer.set(key, read<MixerStrip>(key) as MixerStrip);
        break;
      case "meter":
        // Straight from authoritative: a meter is never pending (B58).
        if (id !== null) meters.set(id, authoritative.get(key) as readonly (number | null)[]);
        break;
      case "device":
        devices.set(tail, read<DeviceState>(key) as DeviceState);
        break;
      default:
        break;
    }
  }
  return {
    lighting: {
      levels,
      colours,
      master: getMaster(),
      bindings,
      observed,
      externalControl: getExternalControl(),
    },
    mixer,
    meters,
    devices,
    scenes: getScenes(),
    surface: getSurface(),
    timer: getTimerState(),
    banners: getBanners(),
    connection: getConnectionState(),
  };
}

/**
 * The authoritative values whose keys pass `keep`, for `@/offline/lastKnown`
 * to save as last-known values (§21.27). Never a pending value: a gesture
 * nobody confirmed is not something the room was ever known to be at.
 */
export function authoritativeEntries(keep: (key: Key) => boolean): Array<[Key, unknown]> {
  const entries: Array<[Key, unknown]> = [];
  for (const [key, value] of authoritative) if (keep(key)) entries.push([key, value]);
  return entries;
}

/**
 * Put saved last-known values back after a cold start while the controller
 * is unreachable (§21.27, "never blank"). A key a frame has already written
 * is left alone — anything the controller said outranks a saved copy — and
 * the socket's resync replaces the rest the moment it connects.
 */
export function seedAuthoritative(entries: Iterable<readonly [Key, unknown]>): void {
  applyFrame(() => {
    for (const [key, value] of entries) {
      if (authoritative.has(key)) continue;
      put(key, value !== null && typeof value === "object" ? Object.freeze(value) : value);
    }
  });
}

/** Test and reset helper: back to an empty store with the listeners still attached. */
export function resetLiveState(): void {
  applyFrame(() => {
    for (const key of [...authoritative.keys()]) {
      authoritative.delete(key);
      touch(key);
    }
    for (const key of [...pending.keys()]) {
      pending.delete(key);
      touch(key);
    }
    for (const key of [...feedbacks.keys()]) {
      feedbacks.delete(key);
      touch(feedbackKey(key));
    }
    tokens.clear();
    gestures.clear();
  });
}
