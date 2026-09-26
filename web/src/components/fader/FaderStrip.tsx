/*
 * The vertical touch fader (spec §10.3, §21.2, §21.3, §21.4, §21.5, §24.2).
 * Built on the Pointer Events API — not mouse or touch events — for
 * sub-pixel tracking and genuine multi-touch: each `FaderStrip` captures its
 * own pointer, so two can move under two fingers at once with no shared
 * state between them.
 *
 * This component is deliberately domain-agnostic. It knows nothing about
 * lighting, channels or dB — it takes a `value`, a `FaderScale` to convert
 * that value to and from a position, and calls back. The live store, gesture
 * arbitration against authoritative frames, and the WebSocket write all live
 * one layer up, in the component that owns a particular channel (§21.2
 * "FaderStrip subscribes to its own channel" — that composition happens
 * where the channel is known, not in here).
 *
 * Two things this component does own:
 *   - The 0–100 % throttle on `onChange` while dragging (§21.2 "coalesced to
 *     about 30 per second"), so every caller gets that for free rather than
 *     re-implementing it per domain. The thumb itself still tracks the
 *     pointer every move — only the callback is paced.
 *   - Keyboard and ARIA exactly as §24.2 specifies, including its corrected
 *     example: aria-valuenow carries position (0–1000), aria-valuetext the
 *     scale's readable value, because a level is not linear in position.
 */
import { useRef, useState, type CSSProperties, type KeyboardEvent, type PointerEvent } from "react";

import { cn } from "@/lib/utils";

import type { FaderScale } from "./FaderScale";

/** §21.2: a drag's writes are coalesced to about 30 per second. */
const DRAG_THROTTLE_MS = 1000 / 30;

/** A pointer past this fraction of the track before release is a drag, not a stray tap. */
function clamp01(value: number): number {
  return Math.min(1, Math.max(0, value));
}

function now(): number {
  return typeof performance !== "undefined" ? performance.now() : Date.now();
}

export type SceneRing = "normal" | "critical";

export interface FaderStripProps {
  /**
   * The printed label and the subject of the accessible name. §24.2's
   * corrected example sets `aria-label="Wireless Mic 1 fader"` — the word
   * "fader" is appended for the accessible name only, so a screen reader
   * announces what kind of control this is; the printed label stays just
   * the channel's own name.
   */
  label: string;
  /**
   * The set value, in the scale's own units — never a position (§5.5, B41).
   * `null` is a mixer channel that is off; lighting never passes it.
   */
  value: number | null;
  scale: FaderScale;
  /** Fired on every pointer move (throttled), on release (flushed), and on a keyboard step. */
  onChange: (value: number | null) => void;
  /** Pointer down: the caller begins a gesture against the live store (§21.2). */
  onGestureStart?: () => void;
  /** Pointer up: the caller ends the gesture; `value` is the final position dragged to. */
  onGestureEnd?: (value: number | null) => void;
  /**
   * The composited/observed value for the ghost mark (§9.4): "what the
   * fixture is doing", distinct from the thumb's "what was set". `null`
   * when there is nothing to show (external control's own faders carry the
   * observed level as `value` directly and pass no ghost).
   */
  ghostValue?: number | null;
  /** Extends the cover to full height, hiding the gradient; the strip drops to 60% opacity. */
  muted?: boolean;
  /** The write would be rejected outright (a critical scene); the control says so. */
  disabled?: boolean;
  /** External control: observed levels, live and read-only (§21.11, §7.2.7). */
  readOnly?: boolean;
  /** Manual external control: last-known controller values, shown dimmed (§21.11). */
  reduced?: boolean;
  /** RGB/RGBW fixture fill (§21.11): a CSS colour built at runtime, never a literal in source. */
  fillColour?: string | undefined;
  /**
   * A hirer's ceiling on this channel, in the scale's own units (§15.4, §18
   * Q4, Q8, Q9): a line drawn on the track, and the top of travel — drag and
   * the keyboard steps both stop there, so the limit reads as designed
   * rather than as a fader that silently refuses to go further. `null`/absent
   * is no ceiling; staff views never pass one.
   */
  ceiling?: number | null;
  /** A scene driving this channel (§10.6, §21.11): pulsing teal, or red and disabled under a critical scene. */
  sceneRing?: SceneRing | null;
  /** A small chip beside the readout distinguishing an observed value from the controller's own (§21.11). */
  liveChip?: boolean;
  /**
   * Print the scale's legend beside the track (§5.5 "the table also produces
   * the printed scale", §21.13): the mixer's scale comes from the driver's
   * law, so the marks move with the desk. Lighting's strips have no room for
   * one and pass nothing.
   */
  showScale?: boolean;
  className?: string;
  testId?: string;
}

export function FaderStrip({
  label,
  value,
  scale,
  onChange,
  onGestureStart,
  onGestureEnd,
  ghostValue = null,
  muted = false,
  disabled = false,
  readOnly = false,
  reduced = false,
  fillColour,
  sceneRing = null,
  liveChip = false,
  showScale = false,
  ceiling = null,
  className,
  testId,
}: FaderStripProps) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const [dragPosition, setDragPosition] = useState<number | null>(null);
  const activePointer = useRef<number | null>(null);
  const lastSent = useRef(0);

  // A critical scene's write would be rejected outright, so the control says
  // so rather than accepting a gesture it will discard (§21.11).
  const effectiveDisabled = disabled || sceneRing === "critical";
  const interactive = !effectiveDisabled && !readOnly;

  // The top of travel a ceiling allows, in position terms (0.0–1.0). Position
  // is monotonic with value on every scale this component is given, so
  // capping position at this point is equivalent to capping the value at the
  // ceiling — and it is what lets one clamp cover both the drag and the
  // initial pointer-down jump through `positionFromClientY` below.
  const ceilingPosition = ceiling !== null && ceiling !== undefined ? scale.toPosition(ceiling) : null;

  function capToCeiling(pos: number): number {
    return ceilingPosition !== null ? Math.min(pos, ceilingPosition) : pos;
  }

  /** A value clamped to the ceiling, where one applies; passed through otherwise (off stays off). */
  function capValueToCeiling(next: number | null): number | null {
    if (next === null || ceiling === null || ceiling === undefined) return next;
    return Math.min(next, ceiling);
  }

  const position = capToCeiling(dragPosition ?? scale.toPosition(value));
  const currentValue = dragPosition !== null ? capValueToCeiling(scale.fromPosition(dragPosition)) : value;

  function positionFromClientY(clientY: number): number {
    const rect = wrapRef.current?.getBoundingClientRect();
    if (!rect || rect.height === 0) return position;
    return capToCeiling(clamp01((rect.bottom - clientY) / rect.height));
  }

  function commit(pos: number, flush: boolean): void {
    // `pos` already sits at or below the ceiling position (`positionFromClientY`
    // capped it); this second clamp only guards a scale whose detent snap
    // could otherwise nudge the resolved value a touch past it.
    const next = capValueToCeiling(scale.fromPosition(pos));
    const t = now();
    if (flush || t - lastSent.current >= DRAG_THROTTLE_MS) {
      lastSent.current = t;
      onChange(next);
    }
  }

  function handlePointerDown(event: PointerEvent<HTMLDivElement>): void {
    if (!interactive) return;
    event.currentTarget.focus();
    try {
      event.currentTarget.setPointerCapture(event.pointerId);
    } catch {
      // Progressive enhancement only: older engines (and jsdom) may not
      // implement pointer capture. The drag still works from move events.
    }
    activePointer.current = event.pointerId;
    const pos = positionFromClientY(event.clientY);
    setDragPosition(pos);
    onGestureStart?.();
    commit(pos, true);
  }

  function handlePointerMove(event: PointerEvent<HTMLDivElement>): void {
    if (activePointer.current !== event.pointerId) return;
    const pos = positionFromClientY(event.clientY);
    setDragPosition(pos);
    commit(pos, false);
  }

  function endPointer(event: PointerEvent<HTMLDivElement>): void {
    if (activePointer.current !== event.pointerId) return;
    // Always computed fresh from this event's own coordinates, rather than
    // trusting a `dragPosition` left over from the last move: the release
    // event carries the authoritative final position, and a caller may
    // release without an intervening move landing exactly there.
    const pos = positionFromClientY(event.clientY);
    const finalValue = capValueToCeiling(scale.fromPosition(pos));
    activePointer.current = null;
    setDragPosition(null);
    onChange(finalValue); // always flushed, whatever the throttle window
    onGestureEnd?.(finalValue);
  }

  function handleKeyDown(event: KeyboardEvent<HTMLDivElement>): void {
    if (!interactive) return;
    let next: number | null;
    switch (event.key) {
      case "ArrowUp":
        next = scale.keyStep(value, 1);
        break;
      case "ArrowDown":
        next = scale.keyStep(value, -1);
        break;
      case "PageUp":
        next = scale.keyPageStep(value, 1);
        break;
      case "PageDown":
        next = scale.keyPageStep(value, -1);
        break;
      case "Home":
        next = scale.min;
        break;
      case "End":
        next = scale.max;
        break;
      default:
        return;
    }
    event.preventDefault();
    // null (off) is already the floor of travel and needs no clamping.
    const bounded = next === null ? null : Math.min(scale.max, Math.max(scale.min, next));
    // The ceiling stops every keyboard step exactly as it stops a drag (§18
    // Q4, Q9) — one Arrow Up at the ceiling holds there rather than nacking,
    // and End (the top of travel) lands on the ceiling rather than overshooting it.
    onChange(capValueToCeiling(bounded));
  }

  const thumbPosition = `${(position * 100).toFixed(3)}%`;
  const coverHeight = `${((1 - position) * 100).toFixed(3)}%`;
  const ghostPosition = ghostValue !== null && ghostValue !== undefined ? `${(scale.toPosition(ghostValue) * 100).toFixed(3)}%` : null;
  const ceilingMarkPosition = ceilingPosition !== null ? `${(ceilingPosition * 100).toFixed(3)}%` : null;

  const valueText = scale.format(currentValue);
  // §24.2: the spoken value can differ from the printed readout ("−5.0
  // decibels" vs "-5.0"). Where a scale carries no spokenFormat — lighting's
  // own percentage already reads fine aloud — this is simply valueText again.
  const spokenText = scale.spokenFormat ? scale.spokenFormat(currentValue) : valueText;
  const ghostText =
    ghostValue !== null && ghostValue !== undefined && scale.format(ghostValue) !== valueText
      ? `→${scale.format(ghostValue)}`
      : null;

  const fillStyle: CSSProperties | undefined = fillColour ? { background: fillColour } : undefined;

  return (
    <div className={cn("fader-strip", className)} data-testid={testId}>
      <div className="fader-label">{label}</div>
      <div className="fader-readout">
        <span className="fader-value" data-detent={scale.isDetent(currentValue) || undefined}>
          {valueText}
        </span>
        {ghostText ? <span className="fader-ghost-value">{ghostText}</span> : null}
        {liveChip ? <span className="fader-live-chip">LIVE</span> : null}
      </div>
      <div
        ref={wrapRef}
        className={cn("fader-track-wrap", reduced && "is-reduced")}
        role="slider"
        tabIndex={effectiveDisabled ? -1 : 0}
        aria-label={`${label} fader`}
        aria-orientation="vertical"
        aria-valuemin={0}
        aria-valuemax={ceilingPosition !== null ? Math.round(ceilingPosition * 1000) : 1000}
        aria-valuenow={Math.round(position * 1000)}
        aria-valuetext={spokenText}
        aria-disabled={effectiveDisabled || undefined}
        aria-readonly={readOnly || undefined}
        data-scene-ring={sceneRing ?? undefined}
        onPointerDown={handlePointerDown}
        onPointerMove={handlePointerMove}
        onPointerUp={endPointer}
        onPointerCancel={endPointer}
        onKeyDown={handleKeyDown}
      >
        {showScale ? (
          // Decorative for a screen reader: the slider's own value text says
          // the level, and the legend is what the eye aligns it against.
          <div className="fader-scale" aria-hidden="true" data-testid={testId ? `${testId}-scale` : undefined}>
            {scale.ticks().map((tick) => (
              <span
                key={`${tick.position}:${tick.label}`}
                className="fader-scale-mark"
                data-detent={tick.detent || undefined}
                data-position={tick.position}
                style={{ bottom: `${(tick.position * 100).toFixed(3)}%` }}
              >
                {tick.label}
              </span>
            ))}
          </div>
        ) : null}
        <div className="fader-track" data-muted={muted || undefined}>
          <div className="fader-fill" style={fillStyle} />
          <div className="fader-cover" style={{ height: coverHeight }} />
          {ghostPosition !== null ? <div className="fader-ghost-mark" style={{ bottom: ghostPosition }} /> : null}
          {/* The hirer's ceiling (§18 Q4, §21.15): "the limit visible on the
              track so it reads as designed rather than broken". Travel above
              it is dimmed so the cap is legible without a separate legend. */}
          {ceilingMarkPosition !== null ? (
            <>
              <div className="fader-ceiling-cap" style={{ height: `calc(100% - ${ceilingMarkPosition})` }} />
              <div className="fader-ceiling-mark" style={{ bottom: ceilingMarkPosition }} data-testid={testId ? `${testId}-ceiling` : undefined} />
            </>
          ) : null}
          <div className="fader-thumb" data-detent={scale.isDetent(currentValue) || undefined} style={{ bottom: thumbPosition }} />
        </div>
      </div>
    </div>
  );
}
