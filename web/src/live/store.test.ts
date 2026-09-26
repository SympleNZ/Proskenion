/*
 * The live state store (spec §21.2; acceptance in §22.3). These cover the two
 * failure modes the store exists to prevent: a rejection settling on a stale
 * value, and a frame moving a control under a finger.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  abandonWrites,
  applyMessage,
  beginGesture,
  bindingKey,
  clearLamps,
  clearMeters,
  clearPending,
  clearProgress,
  clearProgressFor,
  endGesture,
  getBanners,
  getBinding,
  getColour,
  getDisplayLevel,
  getFeedback,
  getHdmiDestinationState,
  getLamp,
  getLevel,
  getLiveState,
  getMeter,
  getMixer,
  getMixerDb,
  getObserved,
  getPagesChangedIds,
  getProgress,
  getProjectorState,
  getScenes,
  getTimerState,
  hasPending,
  hdmiDestinationKey,
  isSceneRunning,
  levelKey,
  meterKey,
  mixerKey,
  PAGES_CHANGED_KEY,
  resetLiveState,
  resolveAck,
  resolveNack,
  setHdmiDestinationState,
  setLevel,
  setPending,
  setPendingWrite,
  subscribeColour,
  subscribeKey,
  subscribeLamp,
  subscribeLevel,
  subscribeMeter,
  subscribeProgress,
} from "./store";

beforeEach(() => {
  resetLiveState();
});

describe("the pending overlay", () => {
  it("settles a late rejection on the frame's value, not the pre-gesture one", () => {
    // The §21.2 sequence, exactly as it is written there.
    setLevel(7, 50); // before the gesture
    const key = levelKey(7);

    // t=0 — the user drags the fader.
    setPending(key, 82.5, 4413);
    expect(getLevel(7)).toBe(82.5);

    // t=40ms — an authoritative frame arrives with a different value.
    applyMessage({ type: "lighting_state", channels: { "7": { level: 61.2 } }, source: "fade" });
    expect(getLevel(7)).toBe(82.5); // pending still wins; the fader has not moved

    // t=80ms — the server rejects the write.
    resolveNack(4413, "permission_denied");

    // The frame's value, which is current, rather than the pre-gesture 50.
    expect(getLevel(7)).toBe(61.2);
  });

  it("falls through to an already-correct value on acknowledgement", () => {
    setLevel(3, 20);
    const key = levelKey(3);
    setPending(key, 44, 1);
    applyMessage({ type: "lighting_state", channels: { "3": { level: 44 } }, source: "app" });
    const seen: number[] = [];
    subscribeLevel(3, () => seen.push(getLevel(3) as number));
    resolveAck(1);
    // One notification, and no flash through an intermediate value.
    expect(seen).toEqual([44]);
    expect(getLevel(3)).toBe(44);
  });

  it("ignores an acknowledgement for a superseded write", () => {
    const key = levelKey(5);
    setPending(key, 10, 1);
    setPending(key, 20, 2); // the drag moved on
    resolveAck(1);
    expect(getLevel(5)).toBe(20); // still the newest gesture value
    resolveAck(2);
    expect(getLevel(5)).toBeNull();
  });

  it("settles on the authoritative value a nack carries", () => {
    setPendingWrite("mixer", 3, -2.0, 7);
    expect(getMixerDb(3)).toBe(-2.0);
    resolveNack(7, "value_out_of_range", -6.0); // clamped to a hirer ceiling
    expect(getMixerDb(3)).toBe(-6.0);
    expect(getMixer(3)?.muted).toBe(false);
  });

  it("a write to an output or Main lands in that section's key, not the input bucket (P4-T7 fix)", () => {
    // Before the fix, writeKey(mixer, id) always resolved to the input
    // section regardless of the ref passed, so a pending write to an output
    // or to Main was invisible to any component reading mixerKey({section:
    // "output"|"main", ...}) — the exact bucket a `mixer_state` frame fills.
    setPendingWrite("mixer", { section: "output", id: 2 }, -6.0, 21);
    expect(getMixer({ section: "output", id: 2 })?.db).toBe(-6.0);
    expect(getMixer(2)).toBeNull(); // the input bucket for the same numeric id is untouched

    // Main's wire id (its real channel_id) may differ from the store's fixed
    // internal id 0 — the write still lands where applyMixerSection stores it.
    setPendingWrite("mixer", { section: "main", id: 1 }, 0.0, 22);
    expect(getMixer({ section: "main", id: 0 })?.db).toBe(0.0);
  });

  it("records a clamp as a success and a rejection as one", () => {
    setPendingWrite("lighting", 2, 90, 11);
    resolveNack(11, "value_out_of_range", 80);
    expect(getFeedback(levelKey(2))?.kind).toBe("clamped");

    setPendingWrite("lighting", 2, 90, 12);
    resolveNack(12, "permission_denied");
    expect(getFeedback(levelKey(2))?.kind).toBe("rejected");
  });

  it("drops every write in flight on reconnect and replays nothing", () => {
    setLevel(1, 30);
    setPendingWrite("lighting", 1, 90, 21);
    expect(getLevel(1)).toBe(90);
    abandonWrites();
    expect(getLevel(1)).toBe(30);
    // The token is gone with it: a late ack cannot resurrect the gesture.
    expect(resolveAck(21)).toBeNull();
    expect(getLevel(1)).toBe(30);
  });
});

describe("gesture arbitration", () => {
  it("does not move a control under an active pointer, and catches up on release", () => {
    setLevel(4, 10);
    const key = levelKey(4);
    const moved = vi.fn();
    subscribeLevel(4, moved);

    beginGesture(key);
    setPending(key, 55, 1);
    expect(moved).toHaveBeenCalledTimes(1); // the gesture itself
    moved.mockClear();

    // A scene runs during the gesture (§10.6). The frame is written silently.
    applyMessage({ type: "lighting_state", channels: { "4": { level: 88 } }, source: "scene" });
    expect(getLevel(4)).toBe(55);
    expect(moved).not.toHaveBeenCalled();

    // Acknowledged while the pointer is still down: the fader stays put.
    resolveAck(1);
    expect(getLevel(4)).toBe(55);

    // Pointer up: it settles on what the server actually holds. Visibly
    // moving here is correct and honest.
    endGesture(key);
    expect(getLevel(4)).toBe(88);
    expect(moved).toHaveBeenCalledTimes(1);
  });

  it("keeps the gesture value until the write is answered", () => {
    const key = levelKey(6);
    setLevel(6, 12);
    beginGesture(key);
    setPending(key, 70, 2);
    endGesture(key); // released before the ack arrives
    expect(getLevel(6)).toBe(70);
    resolveAck(2);
    expect(getLevel(6)).toBe(12);
  });
});

describe("per-key notification", () => {
  it("wakes only the channels a frame touched", () => {
    const one = vi.fn();
    const two = vi.fn();
    const colour = vi.fn();
    subscribeLevel(1, one);
    subscribeLevel(2, two);
    subscribeColour(1, colour);

    applyMessage({ type: "lighting_state", channels: { "1": { level: 85.0 } }, source: "fade" });

    expect(one).toHaveBeenCalledTimes(1);
    expect(two).not.toHaveBeenCalled();
    expect(colour).not.toHaveBeenCalled();
  });

  it("notifies once per key however many changes a frame carries", () => {
    const one = vi.fn();
    subscribeLevel(1, one);
    applyMessage({
      type: "lighting_state",
      channels: { "1": { level: 85.0, r: 255, g: 120, b: 0 }, "2": { level: 78.5 } },
      groups: { "1": 0.85 },
      master: 100.0,
      source: "fade",
    });
    expect(one).toHaveBeenCalledTimes(1);
  });

  it("says nothing when a frame repeats a value", () => {
    applyMessage({ type: "lighting_state", channels: { "9": { level: 40 } }, source: "fade" });
    const quiet = vi.fn();
    subscribeLevel(9, quiet);
    applyMessage({ type: "lighting_state", channels: { "9": { level: 40 } }, source: "fade" });
    expect(quiet).not.toHaveBeenCalled();
  });

  it("keeps a colour's reference between frames that do not change it", () => {
    applyMessage({ type: "lighting_state", channels: { "2": { level: 78.5, r: 255, g: 120, b: 0 } } });
    const first = getColour(2);
    applyMessage({ type: "lighting_state", channels: { "2": { level: 40, r: 255, g: 120, b: 0 } } });
    // Referentially stable, or useSyncExternalStore re-renders forever.
    expect(getColour(2)).toBe(first);
    applyMessage({ type: "lighting_state", channels: { "2": { r: 255, g: 121, b: 0 } } });
    expect(getColour(2)).not.toBe(first);
    expect(getColour(2)?.g).toBe(121);
  });
});

describe("meters", () => {
  it("are absent rather than at the floor", () => {
    applyMessage({ type: "mixer_meters", channels: { "1": [-12.4], "3": [-18.2, -17.9] }, at: 1757462011.482 });
    expect(getMeter(1)).toEqual([-12.4]);
    expect(getMeter(3)).toEqual([-18.2, -17.9]); // index 0 is left
    expect(getMeter(2)).toBeNull(); // no bar, rather than a bar at the bottom
  });

  it("live beside the mixer state, never inside it", () => {
    applyMessage({ type: "mixer_state", inputs: { "1": { db: -5.0, muted: false, origin: "mixpad" } } });
    applyMessage({ type: "mixer_meters", channels: { "1": [-12.4] } });
    expect(getMixer(1)).toEqual({ db: -5.0, muted: false, origin: "mixpad" });
    expect(getLiveState().meters.get(1)).toEqual([-12.4]);
  });

  it("are never replayed on resync — a stale meter is worse than none", () => {
    applyMessage({ type: "mixer_meters", channels: { "1": [-12.4] } });
    const gone = vi.fn();
    subscribeMeter(1, gone);
    clearMeters();
    expect(getMeter(1)).toBeNull();
    expect(gone).toHaveBeenCalledTimes(1);
  });

  it("are never written to pending", () => {
    // There is no gesture that produces a meter, and nothing in the control
    // path may read one (§5.5, B58).
    expect(() => setPending(meterKey(1), [-10])).toThrow(/never written to pending/);
  });
});

describe("lamps (P5-T4's status frame, phase-5-contracts.md)", () => {
  it("reads a status frame's lamps into the lamp store, keyed by state_id", () => {
    applyMessage({ type: "status", lamps: { "4": { on: true, transitioning: false }, "7": { on: false, transitioning: true } } });
    expect(getLamp(4)).toEqual({ on: true, transitioning: false });
    expect(getLamp(7)).toEqual({ on: false, transitioning: true });
    expect(getLamp(9)).toBeNull(); // no frame has said anything about this one yet
  });

  it("carries on: null through — a derived status with no answer yet", () => {
    applyMessage({ type: "status", lamps: { "4": { on: null, transitioning: false } } });
    expect(getLamp(4)).toEqual({ on: null, transitioning: false });
  });

  it("are never replayed on resync — which lamp a connection may see can shrink", () => {
    applyMessage({ type: "status", lamps: { "4": { on: true, transitioning: false } } });
    const gone = vi.fn();
    subscribeLamp(4, gone);
    clearLamps();
    expect(getLamp(4)).toBeNull();
    expect(gone).toHaveBeenCalledTimes(1);
  });

  it("only notifies a lamp's own listeners, not every lamp, on a partial frame", () => {
    applyMessage({ type: "status", lamps: { "4": { on: false, transitioning: false } } });
    const four = vi.fn();
    const seven = vi.fn();
    subscribeLamp(4, four);
    subscribeLamp(7, seven);
    applyMessage({ type: "status", lamps: { "7": { on: true, transitioning: false } } });
    expect(four).not.toHaveBeenCalled();
    expect(seven).toHaveBeenCalledTimes(1);
  });
});

describe("progress (P6-T23)", () => {
  it("reads a progress frame, keyed by operation", () => {
    applyMessage({ type: "progress", operation: "backup_run", step: 2, of: 4, message: "Running the backup job" });
    expect(getProgress("backup_run")).toEqual({ operation: "backup_run", step: 2, of: 4, message: "Running the backup job" });
    expect(getProgress("cert_issue")).toBeNull();
  });

  it("is never replayed on resync — a card left reading 'step < of' forever is worse than one that briefly shows nothing", () => {
    applyMessage({ type: "progress", operation: "backup_run", step: 2, of: 4, message: "Running the backup job" });
    const gone = vi.fn();
    subscribeProgress("backup_run", gone);
    clearProgress();
    expect(getProgress("backup_run")).toBeNull();
    expect(gone).toHaveBeenCalledTimes(1);
  });

  it("clears every operation's progress, not only one — the same resync serves every screen", () => {
    applyMessage({ type: "progress", operation: "backup_run", step: 2, of: 4, message: "Running the backup job" });
    applyMessage({ type: "progress", operation: "cert_issue", step: 1, of: 6, message: "Requesting a certificate" });
    clearProgress();
    expect(getProgress("backup_run")).toBeNull();
    expect(getProgress("cert_issue")).toBeNull();
  });

  it("clearProgressFor clears only the named operation, unlike clearProgress's every-operation sweep", () => {
    applyMessage({ type: "progress", operation: "backup_run", step: 2, of: 4, message: "Running the backup job" });
    applyMessage({ type: "progress", operation: "cert_issue", step: 1, of: 6, message: "Requesting a certificate" });
    clearProgressFor("backup_run");
    expect(getProgress("backup_run")).toBeNull();
    expect(getProgress("cert_issue")).toEqual({
      operation: "cert_issue",
      step: 1,
      of: 6,
      message: "Requesting a certificate",
    });
  });

  it("clearProgressFor notifies a subscriber of the named operation once, and does nothing when there is nothing to clear", () => {
    applyMessage({ type: "progress", operation: "backup_run", step: 2, of: 4, message: "Running the backup job" });
    const gone = vi.fn();
    subscribeProgress("backup_run", gone);
    clearProgressFor("backup_run");
    expect(gone).toHaveBeenCalledTimes(1);
    // Nothing was ever set for "cert_issue" — clearing it must not notify a
    // subscriber of a change that never happened.
    const untouched = vi.fn();
    subscribeProgress("cert_issue", untouched);
    clearProgressFor("cert_issue");
    expect(untouched).not.toHaveBeenCalled();
  });
});

describe("pages_changed (Phase 5 contracts, \"Additions\"; P5-T4)", () => {
  it("carries the frame's page_ids", () => {
    applyMessage({ type: "pages_changed", page_ids: [1, 3] });
    expect(getPagesChangedIds()).toEqual([1, 3]);
  });

  it("notifies on every frame, even with the same ids — a second edit still refetches", () => {
    applyMessage({ type: "pages_changed", page_ids: [1] });
    const listener = vi.fn();
    subscribeKey(PAGES_CHANGED_KEY, listener);
    applyMessage({ type: "pages_changed", page_ids: [1] });
    expect(listener).toHaveBeenCalledTimes(1);
  });

  it("ignores a non-numeric id rather than crashing", () => {
    applyMessage({ type: "pages_changed", page_ids: [1, "oops", 3] });
    expect(getPagesChangedIds()).toEqual([1, 3]);
  });
});

describe("observed levels", () => {
  it("stay a separate map and are never merged with levels", () => {
    applyMessage({
      type: "lighting_state",
      channels: { "1": { level: 40 } },
      external_control: "detected",
      observed: { "1": 85.5 },
    });
    expect(getLevel(1)).toBe(40); // the controller's own model, still there
    expect(getObserved(1)).toBe(85.5);
    expect(getDisplayLevel(1)).toBe(85.5); // the display rule picks observed

    const state = getLiveState();
    expect(state.lighting.levels.get(1)).toBe(40);
    expect(state.lighting.observed.get(1)).toBe(85.5);
  });

  it("clear when the frame says nothing is observed", () => {
    applyMessage({ type: "lighting_state", external_control: "detected", observed: { "1": 85.5 } });
    applyMessage({ type: "lighting_state", external_control: "off", observed: null });
    expect(getObserved(1)).toBeNull();
    expect(getDisplayLevel(1)).toBeNull();
  });
});

describe("stage-bank bindings (§7.1, P2-T9)", () => {
  it("reads a lighting_state frame's bindings object into the binding store", () => {
    expect(getBinding(1)).toBe(false); // absent reads as off, never undefined
    applyMessage({ type: "lighting_state", bindings: { "1": true, "4": true, "2": false } });
    expect(getBinding(1)).toBe(true);
    expect(getBinding(2)).toBe(false);
    expect(getBinding(4)).toBe(true);
    expect(getBinding(3)).toBe(false); // never mentioned, still off
    expect(getLiveState().lighting.bindings.get(1)).toBe(true);
  });

  it("wakes only the bank that changed", () => {
    applyMessage({ type: "lighting_state", bindings: { "1": false, "2": false } });
    const one = vi.fn();
    const two = vi.fn();
    subscribeKey(bindingKey(1), one);
    subscribeKey(bindingKey(2), two);
    applyMessage({ type: "lighting_state", bindings: { "1": true, "2": false } });
    expect(one).toHaveBeenCalledTimes(1);
    expect(two).not.toHaveBeenCalled();
  });
});

describe("discrete messages", () => {
  it("computes the timer from started_at and accumulated_ms, never a running count", () => {
    applyMessage({
      type: "timer",
      running: true,
      started_at: "2026-09-04T14:30:00+12:00",
      accumulated_ms: 5_000,
    });
    const timer = getTimerState();
    expect(timer.running).toBe(true);
    expect(timer.startedAt).toBe(Date.parse("2026-09-04T14:30:00+12:00"));
    expect(timer.accumulated).toBe(5_000);
  });

  it("clears a banner with a null text", () => {
    applyMessage({ type: "banner", level: "amber", key: "backup_media_absent", text: "Backup media not detected" });
    expect(getBanners()).toHaveLength(1);
    applyMessage({ type: "banner", level: "amber", key: "backup_media_absent", text: null });
    expect(getBanners()).toEqual([]);
  });

  it("orders banners red over amber over info", () => {
    applyMessage({ type: "banner", level: "info", key: "c", text: "c" });
    applyMessage({ type: "banner", level: "red", key: "a", text: "a" });
    applyMessage({ type: "banner", level: "amber", key: "b", text: "b" });
    expect(getBanners().map((banner) => banner.key)).toEqual(["a", "b", "c"]);
  });

  it("wakes only the device that changed", () => {
    const knx = vi.fn();
    const mixer = vi.fn();
    subscribeKey("device:knx", knx);
    subscribeKey("device:mixer", mixer);
    applyMessage({ type: "device_status", device: "mixer", status: "connected" });
    expect(mixer).toHaveBeenCalledTimes(1);
    expect(knx).not.toHaveBeenCalled();
  });

  it("keeps main, outputs and inputs apart even when their ids collide", () => {
    applyMessage({
      type: "mixer_state",
      main: { db: 0.0, muted: false },
      outputs: { "1": { db: -6.0, muted: false } },
      inputs: { "1": { db: -5.0, muted: false, origin: "mixpad" } },
    });
    expect(getMixer({ section: "main", id: 0 })?.db).toBe(0);
    expect(getMixer({ section: "output", id: 1 })?.db).toBe(-6);
    expect(getMixer({ section: "input", id: 1 })?.db).toBe(-5);
    expect(mixerKey(1)).toBe(mixerKey({ section: "input", id: 1 }));
  });

  it("clears a MixPad badge when the channel's next entry carries no origin", () => {
    applyMessage({ type: "mixer_state", main: { db: -6.0, muted: false, origin: "mixpad" } });
    applyMessage({ type: "mixer_state", inputs: { "4": { db: -20.0, muted: false, origin: "mixpad" } } });
    expect(getMixer({ section: "main", id: 0 })?.origin).toBe("mixpad");
    expect(getMixer(4)?.origin).toBe("mixpad");

    // Our own write, echoed back as the channel's whole entry: no origin (§16.8).
    applyMessage({ type: "mixer_state", main: { db: 0.0, muted: false } });
    applyMessage({ type: "mixer_state", inputs: { "4": { db: -19.0, muted: false } } });
    expect(getMixer({ section: "main", id: 0 })?.origin).toBe("app");
    expect(getMixer(4)).toEqual({ db: -19, muted: false, origin: "app" });
  });

  it("ignores a message it does not know rather than throwing", () => {
    expect(() => applyMessage({ type: "something_newer", value: 1 })).not.toThrow();
    expect(() => applyMessage(null)).not.toThrow();
  });
});

describe("scenes (§21.10, §16.8, P2-T11)", () => {
  it("marks a scene running on scene_started and clears it on scene_completed", () => {
    applyMessage({ type: "scene_started", scene_id: 3, triggered_by: "api:operator" });
    expect(isSceneRunning(3)).toBe(true);

    applyMessage({ type: "scene_completed", scene_id: 3, result: "success" });
    expect(isSceneRunning(3)).toBe(false);
    expect(getScenes().lastResult).toEqual({ sceneId: 3, result: "ok" });
  });

  it("carries partial and failed results through as reported, never re-derived", () => {
    applyMessage({ type: "scene_completed", scene_id: 5, result: "partial" });
    expect(getScenes().lastResult).toEqual({ sceneId: 5, result: "partial" });
    applyMessage({ type: "scene_completed", scene_id: 5, result: "failed" });
    expect(getScenes().lastResult).toEqual({ sceneId: 5, result: "failed" });
  });

  it("a scenes_state frame marks every scene named in `running` as running", () => {
    applyMessage({
      type: "scenes_state",
      running: {
        "3": { run_id: 1, priority: "normal", triggered_by: "api:operator", channels: [1, 2] },
      },
      last_result: null,
      source: "resync",
    });
    expect(isSceneRunning(3)).toBe(true);
    expect(isSceneRunning(7)).toBe(false);
  });

  it("a mid-scene resync reports the card executing without waiting for scene_started", () => {
    // The gap this closes: a tablet opening its socket while scene 3 is
    // already running previously learned nothing until it happened to
    // complete (§16.8's resync carried no standing value for scenes).
    expect(isSceneRunning(3)).toBe(false);
    applyMessage({
      type: "scenes_state",
      running: { "3": { run_id: 9, priority: "critical", triggered_by: "knx:1/0/1", channels: [] } },
      source: "resync",
    });
    expect(isSceneRunning(3)).toBe(true);
  });

  it("a partial scenes_state frame updates running without touching the last result", () => {
    applyMessage({ type: "scene_completed", scene_id: 5, result: "success" });
    applyMessage({ type: "scenes_state", running: { "9": { run_id: 2 } }, source: "state" });
    expect(isSceneRunning(9)).toBe(true);
    expect(getScenes().lastResult).toEqual({ sceneId: 5, result: "ok" });
  });

  it("a scenes_state frame with an empty running map clears every scene", () => {
    applyMessage({ type: "scene_started", scene_id: 3, triggered_by: "api:operator" });
    applyMessage({ type: "scenes_state", running: {}, last_result: null, source: "resync" });
    expect(isSceneRunning(3)).toBe(false);
  });
});

describe("projector and HDMI (§21.14, phase-3-contracts.md, P3-T7)", () => {
  it("applies the projector_state frame's state and input_ref", () => {
    expect(getProjectorState()).toBeNull();
    applyMessage({ type: "projector_state", state: "warming", input_ref: "31" });
    expect(getProjectorState()).toEqual({ state: "warming", inputRef: "31" });
  });

  it("carries a null input_ref when the frame omits one", () => {
    applyMessage({ type: "projector_state", state: "off" });
    expect(getProjectorState()).toEqual({ state: "off", inputRef: null });
  });

  it("ignores a projector_state frame with no state", () => {
    applyMessage({ type: "projector_state", input_ref: "31" });
    expect(getProjectorState()).toBeNull();
  });

  it("applies the hdmi_source frame per destination, keyed by destination rather than input", () => {
    applyMessage({ type: "hdmi_source", destination_id: 1, input_id: 2, diverged: false });
    expect(getHdmiDestinationState(1)).toEqual({ inputId: 2, diverged: false });
    expect(getHdmiDestinationState(2)).toBeNull(); // a different destination, untouched

    applyMessage({ type: "hdmi_source", destination_id: 1, input_id: 3, diverged: true });
    expect(getHdmiDestinationState(1)).toEqual({ inputId: 3, diverged: true });
  });

  it("wakes only the destination a frame touched", () => {
    const one = vi.fn();
    const two = vi.fn();
    subscribeKey(hdmiDestinationKey(1), one);
    subscribeKey(hdmiDestinationKey(2), two);
    applyMessage({ type: "hdmi_source", destination_id: 1, input_id: 2, diverged: false });
    expect(one).toHaveBeenCalledTimes(1);
    expect(two).not.toHaveBeenCalled();
  });

  it("a pending press clears the moment a frame confirms it — 'the response or the frame' (§21.14)", () => {
    const key = hdmiDestinationKey(1);
    setPending(key, { inputId: 2, diverged: false });
    expect(hasPending(key)).toBe(true);

    applyMessage({ type: "hdmi_source", destination_id: 1, input_id: 2, diverged: false });
    expect(hasPending(key)).toBe(false);
    expect(getHdmiDestinationState(1)).toEqual({ inputId: 2, diverged: false });
  });

  it("a frame also resolves a pending press when it reports a different, out-of-band value", () => {
    // Someone at the front panel changed it while our own press was in
    // flight: the real state is still the truth, and there is nothing left
    // to wait for (§7.5 out-of-band control).
    const key = hdmiDestinationKey(1);
    setPending(key, { inputId: 2, diverged: false });
    applyMessage({ type: "hdmi_source", destination_id: 1, input_id: 5, diverged: false });
    expect(hasPending(key)).toBe(false);
    expect(getHdmiDestinationState(1)).toEqual({ inputId: 5, diverged: false });
  });

  it("clearPending drops an optimistic press without writing to authoritative", () => {
    const key = hdmiDestinationKey(1);
    setHdmiDestinationState(1, { inputId: 1, diverged: false });
    setPending(key, { inputId: 2, diverged: false });
    expect(getHdmiDestinationState(1)).toEqual({ inputId: 2, diverged: false }); // pending wins

    clearPending(key);
    expect(hasPending(key)).toBe(false);
    expect(getHdmiDestinationState(1)).toEqual({ inputId: 1, diverged: false }); // falls back, not stuck
  });
});
