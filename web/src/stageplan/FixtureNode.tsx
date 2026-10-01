/*
 * One SVG fixture, glowing at its displayed level and colour (spec §21.4's
 * LED treatment, §21.5, §21.12, §9.4). A real LED has three visual layers —
 * a bright emitter, a bloom that falls off rapidly, and a specular highlight
 * — and the unlit state matters as much as the lit ones: an unpowered
 * fixture is a dark lamp behind darkened glass, not simply "glow at 0%".
 *
 * This component owns its own gesture recognition — tap, double-tap,
 * long-press and the start of a drag — the same way `FaderStrip` owns its
 * own pointer mechanics: a drag is reported to the caller as raw client
 * coordinates once past a small movement threshold ("a scroll is not a tap",
 * CONVENTIONS), because only the caller (`StagePlan`) knows the SVG's own
 * scale and can turn a client point into a bar and a position.
 */
import { useEffect, useRef, type KeyboardEvent, type PointerEvent } from "react";

import type { LightingChannel } from "@/lighting/types";
import { cn } from "@/lib/utils";

import { NODE_RADIUS } from "./layout";
import type { StagePlanMode } from "./types";

/** A pointer past this many client px before release is a drag or a scroll, not a tap (CONVENTIONS). */
const TAP_MOVE_THRESHOLD = 8;
const DOUBLE_TAP_WINDOW_MS = 350;
const LONG_PRESS_MS = 500;

export interface FixtureNodeProps {
  fixture: LightingChannel;
  /** The displayed level, 0–100 (§7.2.7's display rule already applied by the caller). */
  level: number;
  /** A `#rrggbb` fill built at runtime (never a literal, §21.1) or `null` for a fixture with no colour. */
  colour: string | null;
  selected: boolean;
  /** Whether this viewer may interact with the fixture at all — the hirer lock overlay's hook (§21.12); admin/operator pass `true` for every visible fixture. */
  permitted: boolean;
  mode: StagePlanMode;
  /** A patch overlap involving this fixture (§9.1) — marked with an icon and a word, never colour alone. */
  conflict: boolean;
  /** A DMX fixture under external control: read-only, showing the observed level (§7.2.7). */
  readOnly: boolean;
  /** Manual external control with nothing observed: the controller's last values, dimmed (§7.2.7). */
  reduced: boolean;
  /** An observed value is being shown live — the same LIVE indication as the Lighting view (§21.11). */
  live: boolean;
  /** Admin, not read-only: this node may be dragged and long-pressed (§21.12). */
  draggable: boolean;
  x: number;
  y: number;
  tabIndex: 0 | -1;
  /** This node is the one currently under a drag — rendered ghosted at its origin while a preview follows the pointer. */
  dragGhost?: boolean;
  onTap: (id: number) => void;
  onDoubleTap: (id: number) => void;
  onLongPress?: (id: number, clientX: number, clientY: number) => void;
  onDragStart?: (id: number) => void;
  onDragMove?: (id: number, clientX: number, clientY: number) => void;
  onDragEnd?: (id: number, clientX: number, clientY: number) => void;
  onFocus?: (id: number) => void;
  onKeyDown?: (event: KeyboardEvent<SVGGElement>, id: number) => void;
}

export function FixtureNode({
  fixture,
  level,
  colour,
  selected,
  permitted,
  mode,
  conflict,
  readOnly,
  reduced,
  live,
  draggable,
  x,
  y,
  tabIndex,
  dragGhost = false,
  onTap,
  onDoubleTap,
  onLongPress,
  onDragStart,
  onDragMove,
  onDragEnd,
  onFocus,
  onKeyDown,
}: FixtureNodeProps) {
  const pointerStart = useRef<{ x: number; y: number } | null>(null);
  const moved = useRef(false);
  const dragging = useRef(false);
  const longPressTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const longPressFired = useRef(false);
  const lastTapAt = useRef(0);
  const nodeRef = useRef<SVGGElement>(null);

  // Where a drag can start (edit mode), the touch is the plan's, not the
  // page's scroll: the rest of the drawing lets the page pan (components.css,
  // `.stage-plan-svg`). Chromium does not apply `touch-action` to an element
  // inside an SVG — only to the <svg> itself — so the CSS rule alone left a
  // fixture drag to be cancelled as the page began to scroll. A non-passive
  // `touchstart` that prevents the default is what holds the touch in every
  // engine; pointer events still arrive as before.
  const dragSource = permitted && mode === "admin" && draggable;
  useEffect(() => {
    const node = nodeRef.current;
    if (!node || !dragSource) return undefined;
    const hold = (event: TouchEvent): void => {
      if (event.cancelable) event.preventDefault();
    };
    node.addEventListener("touchstart", hold, { passive: false });
    return () => node.removeEventListener("touchstart", hold);
  }, [dragSource]);

  function clearLongPress(): void {
    if (longPressTimer.current !== null) {
      clearTimeout(longPressTimer.current);
      longPressTimer.current = null;
    }
  }

  function handlePointerDown(event: PointerEvent<SVGGElement>): void {
    try {
      event.currentTarget.setPointerCapture(event.pointerId);
    } catch {
      // Progressive enhancement only: jsdom and older engines may not implement pointer capture.
    }
    pointerStart.current = { x: event.clientX, y: event.clientY };
    moved.current = false;
    dragging.current = false;
    longPressFired.current = false;
    if (mode === "admin" && draggable && onLongPress) {
      const clientX = event.clientX;
      const clientY = event.clientY;
      longPressTimer.current = setTimeout(() => {
        longPressTimer.current = null;
        longPressFired.current = true;
        onLongPress(fixture.id, clientX, clientY);
      }, LONG_PRESS_MS);
    }
  }

  function handlePointerMove(event: PointerEvent<SVGGElement>): void {
    const start = pointerStart.current;
    if (!start) return;
    const dx = event.clientX - start.x;
    const dy = event.clientY - start.y;
    if (!moved.current && Math.hypot(dx, dy) > TAP_MOVE_THRESHOLD) {
      moved.current = true;
      clearLongPress();
      if (mode === "admin" && draggable && onDragStart) {
        dragging.current = true;
        onDragStart(fixture.id);
      }
    }
    if (dragging.current) onDragMove?.(fixture.id, event.clientX, event.clientY);
  }

  function handlePointerUp(event: PointerEvent<SVGGElement>): void {
    clearLongPress();
    const wasDragging = dragging.current;
    dragging.current = false;
    pointerStart.current = null;
    if (wasDragging) {
      onDragEnd?.(fixture.id, event.clientX, event.clientY);
      return;
    }
    if (longPressFired.current) {
      longPressFired.current = false; // the context menu already opened; this release is not also a tap
      return;
    }
    if (moved.current) return; // a scroll is not a tap (CONVENTIONS)
    const now = Date.now();
    if (now - lastTapAt.current <= DOUBLE_TAP_WINDOW_MS) {
      lastTapAt.current = 0;
      onDoubleTap(fixture.id);
    } else {
      lastTapAt.current = now;
      onTap(fixture.id);
    }
  }

  function handlePointerCancel(): void {
    clearLongPress();
    dragging.current = false;
    pointerStart.current = null;
  }

  const roundedLevel = Math.round(level);
  let label = `${fixture.name}, channel ${fixture.id}, at ${roundedLevel} percent`;
  if (!permitted) {
    label = `${fixture.name}, channel ${fixture.id}, locked`;
  } else {
    if (readOnly) label += live ? ", live from the desk" : ", showing the controller's last known values";
    if (conflict) label += ", patch conflict";
  }

  const brightness = Math.max(0, Math.min(1, level / 100));
  const fill = colour ?? "var(--group-white)";
  const nameY = NODE_RADIUS + 20;
  const conflictY = nameY + 18;

  return (
    <g
      ref={nodeRef}
      className={cn(
        "fixture-node-wrap",
        selected && "is-selected",
        reduced && "is-reduced",
        !permitted && "is-locked",
        dragGhost && "is-drag-ghost",
      )}
      transform={`translate(${x} ${y})`}
      role="button"
      aria-label={label}
      aria-pressed={permitted ? selected : undefined}
      aria-disabled={!permitted || undefined}
      aria-readonly={readOnly || undefined}
      tabIndex={permitted ? tabIndex : -1}
      data-fixture-id={fixture.id}
      // Where a drag can start (edit mode): the one place on the plan that
      // takes the touch from the page's scroll (components.css).
      data-drag-source={dragSource || undefined}
      data-testid={`fixture-node-${fixture.id}`}
      onPointerDown={permitted ? handlePointerDown : undefined}
      onPointerMove={permitted ? handlePointerMove : undefined}
      onPointerUp={permitted ? handlePointerUp : undefined}
      onPointerCancel={permitted ? handlePointerCancel : undefined}
      onFocus={permitted ? () => onFocus?.(fixture.id) : undefined}
      onKeyDown={permitted ? (event) => onKeyDown?.(event, fixture.id) : undefined}
    >
      {/* The unlit lamp — visible at every level, brightest layers fade in over it (§21.4). */}
      <circle r={NODE_RADIUS} strokeWidth={2} className="fixture-node-base" />
      <circle
        r={NODE_RADIUS * 1.9}
        filter="url(#fixture-node-bloom-filter)"
        className="fixture-node fixture-node-bloom"
        style={{ fill, opacity: brightness * 0.5 }}
      />
      <circle r={NODE_RADIUS * 0.82} className="fixture-node fixture-node-emitter" style={{ fill, opacity: 0.3 + brightness * 0.7 }} />
      <circle
        cx={-NODE_RADIUS * 0.32}
        cy={-NODE_RADIUS * 0.32}
        r={NODE_RADIUS * 0.22}
        className="fixture-node fixture-node-highlight"
        style={{ opacity: brightness * 0.7 }}
      />
      {selected ? <circle r={NODE_RADIUS + 6} strokeWidth={3} className="fixture-node-selection-ring" /> : null}
      {!permitted ? (
        <text className="fixture-node-lock" textAnchor="middle" dominantBaseline="central" aria-hidden="true">
          🔒
        </text>
      ) : null}
      {live ? (
        <text className="fixture-node-live" y={-(NODE_RADIUS + 12)} textAnchor="middle" aria-hidden="true">
          LIVE
        </text>
      ) : null}
      <text className="fixture-node-label" y={nameY} textAnchor="middle" aria-hidden="true">
        {fixture.name}
      </text>
      {conflict ? (
        <text className="fixture-node-conflict" y={conflictY} textAnchor="middle" aria-hidden="true">
          ⚠ Conflict
        </text>
      ) : null}
    </g>
  );
}
