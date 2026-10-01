/*
 * The touch fader (spec §10.3, §21.2, §21.3, §21.4, §21.5, §24.2), and the
 * channel strip card it sits in (§21.5 "FaderStrip — vertical fader + label
 * + value + mute"; `docs/operator-views.html` is the drawn reference).
 * Built on the Pointer Events API — not mouse or touch events — for sub-pixel
 * tracking and genuine multi-touch: each `FaderStrip` captures its own
 * pointer, so two can move under two fingers at once with no shared state
 * between them.
 *
 * This component is deliberately domain-agnostic. It knows nothing about
 * lighting, channels or dB — it takes a `value`, a `FaderScale` to convert
 * that value to and from a position, and calls back. The live store, gesture
 * arbitration against authoritative frames, and the WebSocket write all live
 * one layer up, in the component that owns a particular channel (§21.2
 * "FaderStrip subscribes to its own channel" — that composition happens
 * where the channel is known, not in here). What a domain adds to the card —
 * a meter, a mute or a bump, a pan control, a badge — arrives through slots.
 *
 * The vertical strip, top to bottom (one layout for every view that shows a
 * fader, so the Mixer, Lighting and Pages views cannot drift apart again):
 *
 *   accent       the group colour along the top edge (identity only, §21.3)
 *   head         name, then a sub-label ("ip1", "group", "U1·12")
 *   zone         the printed scale · the fader · the meter slot — takes every
 *                pixel the card has left, so a taller card is a longer fader
 *   readout      the value, below the fader
 *   foot         the slot for mute / bump / pan / hints
 *
 * The thumb travels inside `.fader-travel`, a box inset from the zone by half
 * the thumb's own height at each end, so at either end of travel the thumb
 * stays inside the zone: it can never sit on the label above or on the
 * readout and button below. Pointer positions are read against that same box,
 * so what the finger touches and where the thumb is drawn are the one
 * coordinate system, and the printed scale is laid out on the same inset.
 *
 * A fader row scrolls sideways (the Lighting rows, the Pages surface, the
 * phone's Mixer desk), so a touch is not a mouse press. The hit area carries
 * `touch-action: pan-x` (`pan-y` on its side), leaving the swipe along the
 * row to the browser; a touch on the track holds back its tap-to-position
 * jump until it is known to be a tap (lifted inside the slop) or a drag
 * along the fader (that axis passed the slop first), and a touch that turns
 * out to be a swipe never changes the level — the browser's `pointercancel`
 * finds nothing to end. A touch on the thumb grabs at once, without jumping.
 * Mouse and pen are unchanged: a press is a press (`touchIntent.ts`).
 *
 * Two things this component does own:
 *   - The 0–100 % throttle on `onChange` while dragging (§21.2 "coalesced to
 *     about 30 per second"), so every caller gets that for free rather than
 *     re-implementing it per domain. The thumb itself still tracks the
 *     pointer every move — only the callback is paced.
 *   - Keyboard and ARIA exactly as §24.2 specifies, including its corrected
 *     example: aria-valuenow carries position (0–1000), aria-valuetext the
 *     scale's readable value, because a level is not linear in position.
 *
 * `orientation="horizontal"` is the same control laid on its side — the
 * Lighting header's Master (§21.11's wireframe draws it as a slider beside
 * Fade) — with no card: label, track, readout in one row.
 */
import {
  useRef,
  useState,
  type CSSProperties,
  type DragEvent,
  type KeyboardEvent,
  type PointerEvent,
  type ReactNode,
} from "react";

import { cn } from "@/lib/utils";

import type { FaderScale } from "./FaderScale";
import { classifyTouch, type TouchIntent } from "./touchIntent";

/** §21.2: a drag's writes are coalesced to about 30 per second. */
const DRAG_THROTTLE_MS = 1000 / 30;

/** One finger on the fader, while its direction is decided (`touchIntent.ts`) and after. */
interface TouchGesture {
  pointerId: number;
  startX: number;
  startY: number;
  intent: TouchIntent;
  /** It landed on the thumb: the gesture began at once, and the thumb keeps its offset from the finger. */
  grab: boolean;
  /** The finger's distance from the thumb's centre along the travel, as a fraction of it. */
  offset: number;
  /** Where the thumb was when the finger landed, to put it back if the touch turns into a scroll. */
  startPosition: number;
}

function clamp01(value: number): number {
  return Math.min(1, Math.max(0, value));
}

function now(): number {
  return typeof performance !== "undefined" ? performance.now() : Date.now();
}

function percent(position: number): string {
  return `${(position * 100).toFixed(3)}%`;
}

export type SceneRing = "normal" | "critical";
export type FaderOrientation = "vertical" | "horizontal";

export interface FaderStripProps {
  /**
   * The printed label and the subject of the accessible name. §24.2's
   * corrected example sets `aria-label="Wireless Mic 1 fader"` — the word
   * "fader" is appended for the accessible name only, so a screen reader
   * announces what kind of control this is; the printed label stays just
   * the channel's own name.
   */
  label: string;
  /** A second, smaller line under the name — a reference or a kind ("ip1", "group"). */
  sublabel?: string | undefined;
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
   * law, so the marks move with the desk. Lighting's strips pass nothing.
   */
  showScale?: boolean;
  orientation?: FaderOrientation;
  /** The group colour along the card's top edge (§21.3: identity only, never status). */
  accentColour?: string | undefined;
  /** Main's card is outlined a little brighter, as the mock draws it. */
  emphasis?: boolean;
  /**
   * The slot right of the fader: the mixer's meter (§21.13 "a narrow bar
   * down the right of the strip"). Laid out on the same inset as the thumb's
   * travel, so a meter's marks line up with the printed scale. `undefined`
   * leaves no slot and the fader centres (§21.13: when metering is
   * unavailable the bar is absent, not empty).
   */
  meter?: ReactNode;
  /**
   * A few words after the value, on the readout's own line — Main's "Fader
   * pos." (§21.13) — so annotating one strip never shortens its fader
   * against its neighbours'.
   */
  readoutNote?: string | undefined;
  /** Top-right of the card: the origin badge (§21.13). */
  corner?: ReactNode;
  /** Below the readout: mute, pan, a hint — whatever the domain adds. */
  children?: ReactNode;
  className?: string;
  /** On the card itself. */
  testId?: string;
  /** On the slider; also the prefix for `-scale` and `-ceiling`. Defaults to `testId`. */
  faderTestId?: string;
}

export function FaderStrip({
  label,
  sublabel,
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
  orientation = "vertical",
  accentColour,
  emphasis = false,
  meter,
  readoutNote,
  corner,
  children,
  className,
  testId,
  faderTestId = testId,
}: FaderStripProps) {
  const travelRef = useRef<HTMLDivElement>(null);
  const thumbRef = useRef<HTMLDivElement>(null);
  const [dragPosition, setDragPosition] = useState<number | null>(null);
  const activePointer = useRef<number | null>(null);
  /** Where the active pointer last actually was, for a cancel that carries no usable coordinates. */
  const lastPosition = useRef<number | null>(null);
  /** Whether this gesture has sent a level yet; a gesture that never did ends without a write. */
  const sentAny = useRef(false);
  /** A touch whose direction is not yet known, or one already known to be a scroll (`touchIntent.ts`). */
  const touch = useRef<TouchGesture | null>(null);
  const lastSent = useRef(0);
  const horizontal = orientation === "horizontal";

  // A critical scene's write would be rejected outright, so the control says
  // so rather than accepting a gesture it will discard (§21.11).
  const effectiveDisabled = disabled || sceneRing === "critical";
  const interactive = !effectiveDisabled && !readOnly;

  // The top of travel a ceiling allows, in position terms (0.0–1.0). Position
  // is monotonic with value on every scale this component is given, so
  // capping position at this point is equivalent to capping the value at the
  // ceiling — and it is what lets one clamp cover both the drag and the
  // initial pointer-down jump through `positionFromPointer` below.
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

  /**
   * The pointer's place along the travel box — the thumb's own coordinate
   * system, not the hit area around it — unclamped, or `null` before layout.
   */
  function pointerFraction(event: PointerEvent<HTMLDivElement>): number | null {
    const rect = travelRef.current?.getBoundingClientRect();
    if (!rect) return null;
    if (horizontal) return rect.width === 0 ? null : (event.clientX - rect.left) / rect.width;
    return rect.height === 0 ? null : (rect.bottom - event.clientY) / rect.height;
  }

  /** `offset` is a thumb grab's distance from the thumb's centre, so a grabbed thumb does not jump. */
  function positionFromPointer(event: PointerEvent<HTMLDivElement>, offset = 0): number {
    const fraction = pointerFraction(event);
    if (fraction === null) return position;
    return capToCeiling(clamp01(fraction - offset));
  }

  /** Whether a touch landed on the thumb itself, which grabs at once rather than waiting to see the direction. */
  function onThumb(event: PointerEvent<HTMLDivElement>): boolean {
    const rect = thumbRef.current?.getBoundingClientRect();
    if (!rect || rect.width === 0 || rect.height === 0) return false;
    return horizontal
      ? event.clientX >= rect.left && event.clientX <= rect.right
      : event.clientY >= rect.top && event.clientY <= rect.bottom;
  }

  function commit(pos: number, flush: boolean): void {
    // `pos` already sits at or below the ceiling position (`positionFromPointer`
    // capped it); this second clamp only guards a scale whose detent snap
    // could otherwise nudge the resolved value a touch past it.
    const next = capValueToCeiling(scale.fromPosition(pos));
    const t = now();
    if (flush || t - lastSent.current >= DRAG_THROTTLE_MS) {
      lastSent.current = t;
      sentAny.current = true;
      onChange(next);
    }
  }

  function capture(event: PointerEvent<HTMLDivElement>): void {
    try {
      event.currentTarget.setPointerCapture(event.pointerId);
    } catch {
      // Progressive enhancement only: older engines (and jsdom) may not
      // implement pointer capture. The drag still works from move events.
    }
  }

  /** The gesture proper: focus, the caller's arbitration against the live store (§21.2), the thumb under the pointer. */
  function begin(event: PointerEvent<HTMLDivElement>, pos: number): void {
    event.currentTarget.focus();
    activePointer.current = event.pointerId;
    lastPosition.current = pos;
    sentAny.current = false;
    setDragPosition(pos);
    onGestureStart?.();
  }

  function handlePointerDown(event: PointerEvent<HTMLDivElement>): void {
    if (!interactive) return;
    // No text selection and no native drag may start from a fader: a later
    // press on selected text would begin a drag-and-drop, and the browser
    // then cancels this pointer (see `cancelPointer`).
    event.preventDefault();
    capture(event);

    if (event.pointerType === "touch") {
      // The row this fader sits in scrolls sideways under the same finger
      // (`touch-action: pan-x`, components.css). A touch on the track waits
      // to see which way it goes before it jumps the level; a touch on the
      // thumb grabs at once, without moving it (`touchIntent.ts`).
      const grab = onThumb(event);
      const gesture: TouchGesture = {
        pointerId: event.pointerId,
        startX: event.clientX,
        startY: event.clientY,
        intent: "pending",
        grab,
        offset: 0,
        startPosition: position,
      };
      touch.current = gesture;
      if (grab) {
        gesture.offset = (pointerFraction(event) ?? position) - position;
        begin(event, position);
      }
      return;
    }

    const pos = positionFromPointer(event);
    begin(event, pos);
    commit(pos, true);
  }

  function handlePointerMove(event: PointerEvent<HTMLDivElement>): void {
    const gesture = touch.current?.pointerId === event.pointerId ? touch.current : null;
    if (gesture) {
      if (gesture.intent === "across") return; // the row's scroll; the level stays where it was
      if (gesture.intent === "pending") {
        gesture.intent = classifyTouch(event.clientX - gesture.startX, event.clientY - gesture.startY, orientation);
        if (gesture.intent === "across") {
          // A sideways swipe that began on the thumb: put back anything the
          // grab moved, and leave the rest of the gesture to the browser.
          if (gesture.grab && activePointer.current === event.pointerId) {
            lastPosition.current = gesture.startPosition;
            setDragPosition(gesture.startPosition);
            if (sentAny.current) commit(gesture.startPosition, true);
          }
          return;
        }
        if (gesture.intent === "along" && !gesture.grab) {
          // A drag along the fader: the deferred jump happens now, to where
          // the finger is, and the drag tracks it from here.
          const pos = positionFromPointer(event);
          begin(event, pos);
          commit(pos, true);
          return;
        }
        if (!gesture.grab) return; // still inside the slop on the track: nothing yet
      }
    }
    if (activePointer.current !== event.pointerId) return;
    const pos = positionFromPointer(event, gesture?.offset ?? 0);
    lastPosition.current = pos;
    setDragPosition(pos);
    commit(pos, false);
  }

  /** Ends the gesture at `pos`, flushed; `null` ends it where it started, without a write. */
  function finish(pos: number | null): void {
    const finalValue = pos === null ? value : capValueToCeiling(scale.fromPosition(pos));
    activePointer.current = null;
    lastPosition.current = null;
    touch.current = null;
    setDragPosition(null);
    if (pos !== null) onChange(finalValue); // always flushed, whatever the throttle window
    onGestureEnd?.(finalValue);
  }

  function releasePointer(event: PointerEvent<HTMLDivElement>): void {
    const gesture = touch.current?.pointerId === event.pointerId ? touch.current : null;
    if (gesture) {
      if (gesture.intent === "pending" && !gesture.grab) {
        // A tap on the track: the deferred jump, to where the finger lifted.
        touch.current = null;
        const pos = positionFromPointer(event);
        begin(event, pos);
        finish(pos);
        return;
      }
      if (gesture.intent === "across" || (gesture.intent === "pending" && gesture.grab)) {
        // A swipe the browser never claimed (the row had nowhere to scroll),
        // or a touch on the thumb that never moved: no change of level —
        // unless a grab had already sent one, which the swipe put back.
        if (activePointer.current === event.pointerId) finish(sentAny.current ? gesture.startPosition : null);
        touch.current = null;
        return;
      }
    }
    if (activePointer.current !== event.pointerId) return;
    // Computed fresh from the release event's own coordinates, rather than
    // trusting a `dragPosition` left over from the last move: a release
    // carries the authoritative final position, and a caller may release
    // without an intervening move landing exactly there.
    finish(positionFromPointer(event, gesture?.offset ?? 0));
  }

  /**
   * The browser took the pointer away — a native drag began, or it claimed a
   * touch for itself (the row's sideways scroll). A cancel's coordinates are
   * not where the pointer was: Chrome reports clientX/clientY as 0, which
   * read as a position is past the top of travel and sent the fader to its
   * maximum (+10 dB on a CQ), with the browser's own drag then swallowing
   * every move that tried to bring it back. The gesture ends where the
   * pointer last actually was; a touch still deciding its direction never
   * started one, so its deferred jump is simply dropped.
   */
  function cancelPointer(event: PointerEvent<HTMLDivElement>): void {
    const gesture = touch.current?.pointerId === event.pointerId ? touch.current : null;
    if (gesture) touch.current = null;
    if (activePointer.current !== event.pointerId) return;
    if (!sentAny.current) {
      finish(null);
      return;
    }
    finish(lastPosition.current ?? position);
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
      case "ArrowRight":
        if (!horizontal) return;
        next = scale.keyStep(value, 1);
        break;
      case "ArrowLeft":
        if (!horizontal) return;
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

  const thumbAt = percent(position);
  const coverSize = percent(1 - position);
  const ghostPosition = ghostValue !== null && ghostValue !== undefined ? percent(scale.toPosition(ghostValue)) : null;
  const ceilingMarkPosition = ceilingPosition !== null ? percent(ceilingPosition) : null;

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
  const along = horizontal ? "left" : "bottom";

  const readout = (
    <div className="fader-readout">
      <span className="fader-value" data-detent={scale.isDetent(currentValue) || undefined}>
        {valueText}
      </span>
      {ghostText ? <span className="fader-ghost-value">{ghostText}</span> : null}
      {liveChip ? <span className="fader-live-chip">LIVE</span> : null}
      {readoutNote !== undefined ? <span className="fader-readout-note">{readoutNote}</span> : null}
    </div>
  );

  const slider = (
    <div
      className={cn("fader-track-wrap", reduced && "is-reduced")}
      role="slider"
      tabIndex={effectiveDisabled ? -1 : 0}
      aria-label={`${label} fader`}
      aria-orientation={orientation}
      aria-valuemin={0}
      aria-valuemax={ceilingPosition !== null ? Math.round(ceilingPosition * 1000) : 1000}
      aria-valuenow={Math.round(position * 1000)}
      aria-valuetext={spokenText}
      aria-disabled={effectiveDisabled || undefined}
      aria-readonly={readOnly || undefined}
      data-scene-ring={sceneRing ?? undefined}
      data-dragging={dragPosition !== null || undefined}
      data-testid={faderTestId}
      onPointerDown={handlePointerDown}
      onPointerMove={handlePointerMove}
      onPointerUp={releasePointer}
      onPointerCancel={cancelPointer}
      onLostPointerCapture={cancelPointer}
      onDragStart={(event: DragEvent<HTMLDivElement>) => event.preventDefault()}
      onKeyDown={handleKeyDown}
    >
      <div ref={travelRef} className="fader-travel">
        <div className="fader-track" data-muted={muted || undefined}>
          <div className="fader-fill" style={fillStyle} />
          <div className="fader-cover" style={horizontal ? { width: coverSize } : { height: coverSize }} />
          {ghostPosition !== null ? <div className="fader-ghost-mark" style={{ [along]: ghostPosition }} /> : null}
          {/* The hirer's ceiling (§18 Q4, §21.15): "the limit visible on the
              track so it reads as designed rather than broken". Travel above
              it is dimmed so the cap is legible without a separate legend. */}
          {ceilingMarkPosition !== null ? (
            <>
              <div
                className="fader-ceiling-cap"
                style={horizontal ? { width: `calc(100% - ${ceilingMarkPosition})` } : { height: `calc(100% - ${ceilingMarkPosition})` }}
              />
              <div
                className="fader-ceiling-mark"
                style={{ [along]: ceilingMarkPosition }}
                data-testid={faderTestId ? `${faderTestId}-ceiling` : undefined}
              />
            </>
          ) : null}
        </div>
        <div ref={thumbRef} className="fader-thumb" data-detent={scale.isDetent(currentValue) || undefined} style={{ [along]: thumbAt }} />
      </div>
    </div>
  );

  if (horizontal) {
    return (
      <div className={cn("fader-strip", className)} data-orientation="horizontal" data-testid={testId}>
        <div className="fader-label">{label}</div>
        {slider}
        {readout}
      </div>
    );
  }

  const cardStyle = accentColour ? ({ "--accent-colour": accentColour } as CSSProperties) : undefined;

  return (
    <div
      className={cn("fader-strip", className)}
      data-orientation="vertical"
      data-emphasis={emphasis || undefined}
      data-muted={muted || undefined}
      style={cardStyle}
      data-testid={testId}
    >
      <div className="fader-strip-accent" aria-hidden="true" />
      {corner}
      <div className="fader-strip-head">
        <div className="fader-label">{label}</div>
        {sublabel !== undefined ? <div className="fader-sublabel">{sublabel}</div> : null}
      </div>
      <div className="fader-zone">
        {showScale ? (
          // Decorative for a screen reader: the slider's own value text says
          // the level, and the legend is what the eye aligns it against.
          <div className="fader-scale" aria-hidden="true" data-testid={faderTestId ? `${faderTestId}-scale` : undefined}>
            <div className="fader-scale-travel">
              {scale.ticks().map((tick) => (
                <span
                  key={`${tick.position}:${tick.label}`}
                  className="fader-scale-mark"
                  data-detent={tick.detent || undefined}
                  data-position={tick.position}
                  style={{ bottom: percent(tick.position) }}
                >
                  {tick.label}
                </span>
              ))}
            </div>
          </div>
        ) : null}
        {slider}
        {meter !== undefined ? <div className="fader-meter-slot">{meter}</div> : null}
      </div>
      {readout}
      {children !== undefined && children !== null ? <div className="fader-strip-foot">{children}</div> : null}
    </div>
  );
}
