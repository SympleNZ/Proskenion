/*
 * One mixer channel strip (spec §21.13): the shared fader on the driver's
 * law, mute, the origin badge, the meter, and pan where the channel shows
 * it. Built entirely on `FaderStrip`/`lawFaderScale` — there is no second
 * fader here, only the composition point that knows this is a mixer
 * channel, exactly as `ChannelFader` is that point for a lighting fixture
 * (§21.2 "FaderStrip subscribes to its own channel").
 *
 * `short_name` is the printed and accessible label: strips are a fixed,
 * non-scrolling width (§21.9, B63), and `short_name` is the field the
 * contract carries specifically sized for that — `name` is reserved for
 * contexts with more room (the desk-scene band, error messages).
 */
import { useState, type KeyboardEvent } from "react";
import { toast } from "sonner";

import { FaderStrip } from "@/components/fader/FaderStrip";
import { lawFaderScale } from "@/components/fader/FaderScale";
import { presentError } from "@/api/errors";
import type { FaderLaw } from "@/lib/faderLaw";
import { send } from "@/live/socket";
import {
  beginGesture,
  clearPending,
  endGesture,
  mixerKey,
  setMixer,
  setPending,
  useControlsEnabled,
  useMeter,
  useMixer,
  type MixerChannel as MixerTarget,
  type MixerStrip,
} from "@/live/store";

import { OriginBadge } from "./OriginBadge";
import { PeakMeter } from "./PeakMeter";
import { useSetMixerMute, useSetMixerPan } from "./api";
import type { MixerCapabilities, MixerOrigin, MixerOutputChannel } from "./types";

/** Why a configured pan control is disabled on a driver without pan (§5.5). */
export const PAN_UNAVAILABLE = "This mixer has no pan control";

export interface ChannelStripChannel extends MixerOutputChannel {
  show_pan?: boolean;
  pan?: number | null;
}

export interface ChannelStripProps {
  channel: ChannelStripChannel;
  /** The store/write target: a bare channel id for an input, a `MixerRef` for an output or Main. */
  target: MixerTarget;
  law: FaderLaw;
  capabilities: MixerCapabilities;
  /** While disconnected, values stay visible but every control is disabled (§21.13). */
  connected: boolean;
  /** Main's dB label is annotated "Fader pos." (§21.13). */
  faderPosLabel?: boolean;
  /** A hirer's ceiling on this channel, in dB (§18 Q4, Q9). Staff views never pass one. */
  ceiling?: number | null;
  /**
   * Pan is refused for a hirer on every path (phase-5-contracts.md: "Always
   * refused for a hirer… pan"), whatever the channel's own `show_pan`
   * configuration — so the control is never drawn here rather than being
   * drawn and always nacked (§21.15 "nothing else is reachable").
   */
  hirer?: boolean;
  testId?: string;
}

/** A compact pan control (§21.13 "a small rotary"), committed on release rather than on every drag tick. */
function PanControl({
  label,
  pan,
  disabled,
  reason,
  onCommit,
}: {
  label: string;
  pan: number;
  disabled: boolean;
  /** Why it is disabled, where that is not simply "disconnected". */
  reason?: string | undefined;
  onCommit: (value: number) => void;
}) {
  // Adjusted during render rather than in an effect (React's own pattern for
  // "reset local state when a prop changes"): a confirmed pan from outside
  // always overrides an uncommitted local drag.
  const [syncedPan, setSyncedPan] = useState(pan);
  const [local, setLocal] = useState(pan);
  if (pan !== syncedPan) {
    setSyncedPan(pan);
    setLocal(pan);
  }

  function commit(): void {
    if (local !== pan) onCommit(local);
  }

  return (
    <input
      type="range"
      className="mixer-pan"
      aria-label={`${label} pan`}
      min={-1}
      max={1}
      step={0.1}
      value={local}
      disabled={disabled}
      title={reason}
      onChange={(event) => setLocal(Number(event.target.value))}
      onPointerUp={commit}
      onKeyUp={(event: KeyboardEvent<HTMLInputElement>) => {
        if (event.key.startsWith("Arrow") || event.key === "Home" || event.key === "End") commit();
      }}
      onBlur={commit}
    />
  );
}

export function ChannelStrip({
  channel,
  target,
  law,
  capabilities,
  connected,
  faderPosLabel = false,
  ceiling = null,
  hirer = false,
  testId,
}: ChannelStripProps) {
  const scale = lawFaderScale(law);
  const key = mixerKey(target);
  const live = useMixer(target);
  const meterValues = useMeter(channel.channel_id);
  const controlsEnabled = useControlsEnabled();
  const setMute = useSetMixerMute();
  const setPan = useSetMixerPan();

  const db = live ? live.db : channel.db;
  const muted = live ? live.muted : channel.muted;
  // The store's Origin ("app" | "mixpad" | "surface") and the contract's
  // MixerOrigin ("mixpad" | "surface" | null) mean the same thing — no
  // badge for an app-originated change — but are spelled differently; this
  // normalises to the contract's shape, which is what `OriginBadge` reads.
  const rawOrigin = live ? live.origin : (channel.origin ?? "app");
  const origin: MixerOrigin = rawOrigin === "mixpad" || rawOrigin === "surface" ? rawOrigin : null;
  const pan = channel.pan ?? null;

  const disabled = !connected || !controlsEnabled;
  const label = channel.short_name || channel.name;

  function handleFaderChange(next: number | null): void {
    send("mixer", target, next);
  }

  function handleMuteToggle(): void {
    const next = !muted;
    const optimistic: MixerStrip = { db, muted: next, origin: origin ?? "app" };
    setPending(key, optimistic);
    setMute.mutate(
      { channelId: channel.channel_id, muted: next },
      {
        onSuccess: (response) => {
          setMixer(target, { db: response.db, muted: response.muted, origin: response.origin ?? "app" });
          clearPending(key);
        },
        onError: (error) => {
          // Dropping the pending overlay is the rollback (§21.27): the
          // control falls back to whatever was already confirmed there —
          // there was never a detour to undo.
          clearPending(key);
          // Hirer notifications are plain (§21.15, §24.6): the one blanket
          // failure message, never the operator's technical error code.
          if (hirer) toast.error("Something went wrong — please speak to venue staff");
          else presentError(error);
        },
      },
    );
  }

  function handlePanCommit(value: number): void {
    setPan.mutate(
      { channelId: channel.channel_id, pan: value },
      { onError: (error) => presentError(error) },
    );
  }

  // §21.13: pan is shown when the channel's configuration sets show_pan. On
  // a driver without pan (§5.5's stub) that configuration is kept and the
  // control is disabled with the reason, not hidden — §5.5 "Capability
  // degradation"; the view prints the reason once, beneath the desk scenes.
  const showPan = channel.show_pan === true && !hirer;
  const panReason = capabilities.pan ? undefined : PAN_UNAVAILABLE;

  return (
    <div className="mixer-channel-strip" data-testid={testId}>
      <OriginBadge origin={origin} />
      <div className="mixer-strip-body">
        <FaderStrip
          label={label}
          value={db}
          scale={scale}
          onChange={handleFaderChange}
          onGestureStart={() => beginGesture(key)}
          onGestureEnd={() => endGesture(key)}
          muted={muted}
          disabled={disabled}
          showScale
          ceiling={ceiling}
          {...(testId ? { testId: `${testId}-fader` } : {})}
        />
        {capabilities.metering ? <PeakMeter values={meterValues} law={law} /> : null}
      </div>
      {faderPosLabel ? <p className="mixer-fader-pos-label">Fader pos.</p> : null}
      {showPan ? (
        <PanControl label={label} pan={pan ?? 0} disabled={disabled || !capabilities.pan} reason={panReason} onCommit={handlePanCommit} />
      ) : null}
      <button
        type="button"
        className="mixer-mute-button"
        aria-pressed={muted}
        aria-label={`Mute ${label}`}
        disabled={disabled}
        onClick={handleMuteToggle}
      >
        Mute
      </button>
    </div>
  );
}
