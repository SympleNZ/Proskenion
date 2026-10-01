/*
 * The stage plan (spec §21.5's `StagePlan`/`FixtureNode`, §21.12, §9.3).
 *
 * This component owns its own mutations and its own selection/drag/context-
 * menu state rather than being purely presentational driven by callback
 * props — the same way `ChannelFader`, `ExternalControlBanner` and
 * `StageBanks` each own their own query/mutation hooks rather than routing
 * everything through a parent. `StagePlanView` (the page-level composition)
 * stays a thin data-loading shell, exactly like `LightingView`.
 *
 * §21.12's capability table, exactly: tap-to-select and double-tap-for-fader
 * work for admin and operator; multi-select, the long-press context menu,
 * dragging a fixture, reordering bars and adding a fixture are admin only.
 */
import { useEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from "react";

import type { Device } from "@/admin/devices/types";
import { ConflictDialog } from "@/admin/devices/ConflictDialog";
import { useCreateBar, useDeleteFixture, useFixtureReferences, useTestFixture, type FixtureTestMode } from "@/admin/lighting/api";
import { ChannelMap } from "@/admin/lighting/ChannelMap";
import type { KnxAddress } from "@/admin/lighting/types";
import { useFixtureSave } from "@/admin/lighting/useFixtureSave";
import { ApiError } from "@/api/client";
import { presentError } from "@/api/errors";
import { Button } from "@/components/ui/Button";
import { Sheet, SheetContent } from "@/components/ui/Sheet";
import { ChannelFader } from "@/lighting/ChannelFader";
import { colourToCss } from "@/lighting/colour";
import { isReadOnlyUnderExternalControl, setLevelTargetsUnderExternalControl } from "@/lighting/externalControl";
import type { FixtureProfile, LightingChannel, LightingGroup } from "@/lighting/types";
import { toLogicalPx } from "@/lib/useDisplayScale";
import { getDisplayLevel, useColour, useDisplayLevel, useExternalControl } from "@/live/store";

import { useCreateGroup, useMoveFixture, useReorderBar, useSetLevels } from "./api";
import { FixtureDeleteDialog } from "./FixtureDeleteDialog";
import { FixtureNode } from "./FixtureNode";
import { FixtureSheet } from "./FixtureSheet";
import { GroupSheet } from "./GroupSheet";
import {
  barIndexFromY,
  barY,
  fixtureX,
  moveRoving,
  orderedBars,
  positionFromX,
  viewHeight,
  VIEW_WIDTH,
  type RovingFixture,
} from "./layout";
import { AddBarDialog } from "./AddBarDialog";
import { MoveConflictDialog } from "./MoveConflictDialog";
import { MoveToBarDialog } from "./MoveToBarDialog";
import { SelectionBar } from "./SelectionBar";
import { SetLevelSheet } from "./SetLevelSheet";
import { proportionalLevels } from "./proportionalLevel";
import { fadeMs } from "@/lighting/fadeTime";
import type { LightingBar, StagePlanMode } from "./types";

export interface StagePlanProps {
  mode: StagePlanMode;
  bars: readonly LightingBar[];
  /** Every channel from `/lighting/channels`; the plan itself filters to what belongs on it. */
  fixtures: readonly LightingChannel[];
  /** Channel ids caught in a patch overlap (§9.1) — built by `conflictChannelIds`. */
  conflicts: ReadonlySet<number>;
  /** Fixture profiles, for the fixture sheet's Type field and live occupancy (§21.18, §15.9). Admin mode only; empty until the caller loads them. */
  profiles?: readonly FixtureProfile[];
  /** Lighting-category output devices (§5.5), for the fixture sheet's DMX device picker. Admin mode only. */
  devices?: readonly Device[];
  /** KNX group addresses (§21.19), for the fixture sheet's KNX dimmer pickers. Admin mode only. */
  knxAddresses?: readonly KnxAddress[];
  /** Every lighting group, for the fixture sheet's delete dialog (group memberships, §9.7). Admin mode only. */
  groups?: readonly LightingGroup[];
}

interface StageFixtureNodeProps {
  fixture: LightingChannel;
  x: number;
  y: number;
  tabIndex: 0 | -1;
  selected: boolean;
  mode: StagePlanMode;
  conflict: boolean;
  draggable: boolean;
  dragGhost?: boolean;
  onTap: (id: number) => void;
  onDoubleTap: (id: number) => void;
  onLongPress?: (id: number, clientX: number, clientY: number) => void;
  onDragStart?: (id: number) => void;
  onDragMove?: (id: number, clientX: number, clientY: number) => void;
  onDragEnd?: (id: number, clientX: number, clientY: number) => void;
  onFocus?: (id: number) => void;
  onKeyDown?: (event: ReactKeyboardEvent<SVGGElement>, id: number) => void;
}

/**
 * The composition point for one fixture (mirrors `ChannelFader`): subscribes
 * to exactly the live-store keys this channel needs and applies §7.2.7's
 * display rule, so `FixtureNode` itself stays presentational.
 */
function StageFixtureNode({ fixture, dragGhost = false, ...rest }: StageFixtureNodeProps) {
  const externalControl = useExternalControl();
  const displayLevel = useDisplayLevel(fixture.id) ?? 0;
  const colourValue = useColour(fixture.id);
  const readOnly = isReadOnlyUnderExternalControl(fixture, externalControl);
  const reduced = readOnly && externalControl === "manual";
  const live = readOnly && externalControl === "detected";
  const colour = fixture.has_colour && colourValue ? colourToCss(colourValue) : null;

  return (
    <FixtureNode
      fixture={fixture}
      level={displayLevel}
      colour={colour}
      permitted
      readOnly={readOnly}
      reduced={reduced}
      live={live}
      dragGhost={dragGhost}
      {...rest}
    />
  );
}

interface FixtureContextMenuProps {
  x: number;
  y: number;
  onOpenFader: () => void;
  onEditFixture: () => void;
  onMoveToBar: () => void;
  onRemoveFromBar: () => void;
  onTestFull: () => void;
  onTestOff: () => void;
  onDelete: () => void;
  onClose: () => void;
}

/**
 * The long-press context menu (§21.12, admin only). §21.18 draws it as: Edit
 * fixture, Move to another bar, Test — full on, Test — off, Delete — with
 * test actions firing immediately, which is how channel assignments get
 * verified at commissioning. "Open fader" and "Remove from bar" predate that
 * drawing and are not in it; they are kept because they do something §21.18's
 * five items do not (a quick fader without leaving the plan, and clearing a
 * fixture's placement without deleting or moving it to a *different* bar).
 */
function FixtureContextMenu({
  x,
  y,
  onOpenFader,
  onEditFixture,
  onMoveToBar,
  onRemoveFromBar,
  onTestFull,
  onTestOff,
  onDelete,
  onClose,
}: FixtureContextMenuProps) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function handlePointerDown(event: globalThis.PointerEvent): void {
      if (ref.current && !ref.current.contains(event.target as Node)) onClose();
    }
    function handleKeyDown(event: globalThis.KeyboardEvent): void {
      if (event.key === "Escape") onClose();
    }
    document.addEventListener("pointerdown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [onClose]);

  // x/y are the long-press's clientX/Y — physical px — while this fixed
  // menu's left/top are laid out inside the display-scaled document (§21.9).
  return (
    <div ref={ref} role="menu" aria-label="Fixture actions" className="stage-plan-context-menu" style={{ left: toLogicalPx(x), top: toLogicalPx(y) }}>
      <button type="button" role="menuitem" className="menu-item" onClick={onOpenFader}>
        Open fader
      </button>
      <button type="button" role="menuitem" className="menu-item" onClick={onEditFixture}>
        Edit fixture
      </button>
      <button type="button" role="menuitem" className="menu-item" onClick={onMoveToBar}>
        Move to another bar
      </button>
      <button type="button" role="menuitem" className="menu-item" onClick={onRemoveFromBar}>
        Remove from bar
      </button>
      <button type="button" role="menuitem" className="menu-item" onClick={onTestFull}>
        Test — full on
      </button>
      <button type="button" role="menuitem" className="menu-item" onClick={onTestOff}>
        Test — off
      </button>
      <button type="button" role="menuitem" className="menu-item menu-item-destructive" onClick={onDelete}>
        Delete
      </button>
    </div>
  );
}

interface BarReorderRowProps {
  bar: LightingBar;
  canMoveUp: boolean;
  canMoveDown: boolean;
  onMoveUp: () => void;
  onMoveDown: () => void;
  /** §21.18: "Add on a bar opens the fixture sheet pre-filled with that bar." */
  onAddFixture: () => void;
  disabled: boolean;
}

function BarReorderRow({ bar, canMoveUp, canMoveDown, onMoveUp, onMoveDown, onAddFixture, disabled }: BarReorderRowProps) {
  return (
    <li className="stage-plan-bar-reorder-row">
      <span>{bar.name}</span>
      <span className="stage-plan-bar-reorder">
        <Button variant="ghost" size="icon" aria-label={`Move ${bar.name} upstage`} disabled={disabled || !canMoveUp} onClick={onMoveUp}>
          ↑
        </Button>
        <Button
          variant="ghost"
          size="icon"
          aria-label={`Move ${bar.name} downstage`}
          disabled={disabled || !canMoveDown}
          onClick={onMoveDown}
        >
          ↓
        </Button>
        <Button variant="ghost" size="icon" aria-label={`Add fixture to ${bar.name}`} disabled={disabled} onClick={onAddFixture}>
          +
        </Button>
      </span>
    </li>
  );
}

export function StagePlan({
  mode,
  bars,
  fixtures,
  conflicts,
  profiles = [],
  devices = [],
  knxAddresses = [],
  groups = [],
}: StagePlanProps) {
  const isAdmin = mode === "admin";
  const svgRef = useRef<SVGSVGElement>(null);
  const announceTimers = useRef<ReturnType<typeof setTimeout>[]>([]);
  const anchorPosition = useRef<number | null>(null);

  const orderedBarsList = useMemo(() => orderedBars(bars), [bars]);
  const barIndexById = useMemo(() => new Map(orderedBarsList.map((bar, index) => [bar.id, index] as const)), [orderedBarsList]);

  const placedFixtures = useMemo(
    () =>
      fixtures.filter(
        (fixture): fixture is LightingChannel & { bar_id: number; position: number } =>
          fixture.visible_staff && fixture.bar_id !== null && fixture.position !== null && barIndexById.has(fixture.bar_id),
      ),
    [fixtures, barIndexById],
  );

  const fixturesById = useMemo(() => new Map(placedFixtures.map((fixture) => [fixture.id, fixture] as const)), [placedFixtures]);

  const externalControl = useExternalControl();
  const [focusedId, setFocusedId] = useState<number | null>(null);
  const rovingId = focusedId !== null && fixturesById.has(focusedId) ? focusedId : (placedFixtures[0]?.id ?? null);

  const [selected, setSelected] = useState<ReadonlySet<number>>(new Set());
  const [faderChannelId, setFaderChannelId] = useState<number | null>(null);
  const [announcement, setAnnouncement] = useState("");
  const [contextMenu, setContextMenu] = useState<{ id: number; x: number; y: number } | null>(null);
  const [dragState, setDragState] = useState<{ id: number; x: number; y: number } | null>(null);
  const [fixtureSheet, setFixtureSheet] = useState<{ fixture: LightingChannel | null; barId: number | null } | null>(null);
  const [groupOpen, setGroupOpen] = useState(false);
  const [setLevelOpen, setSetLevelOpen] = useState(false);
  const [moveToBarTarget, setMoveToBarTarget] = useState<LightingChannel | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<LightingChannel | null>(null);
  // §21.18's toolbar: Edit mode off is live monitoring even for an admin;
  // on enables dragging, the long-press menu and the add buttons. Starts on
  // — an admin opening the plan is usually here to configure it.
  const [editMode, setEditMode] = useState(true);
  const [addBarOpen, setAddBarOpen] = useState(false);
  const [channelMapOpen, setChannelMapOpen] = useState(false);
  // §21.18's Edit mode toggle only narrows what an *admin* can already do —
  // it never widens an operator's or a hirer's capability table (§21.12).
  const editingEnabled = isAdmin && editMode;
  // Remounting the Group/Set-level/Fixture sheets on each open (via `key`) is
  // what seeds their internal state fresh every time — Radix's own
  // `onOpenChange` never fires for an externally controlled `open` prop
  // flipping true, only for a Radix-initiated close, so a reset placed there
  // would never run.
  const [groupOpenSeq, setGroupOpenSeq] = useState(0);
  const [setLevelOpenSeq, setSetLevelOpenSeq] = useState(0);
  const [fixtureSheetSeq, setFixtureSheetSeq] = useState(0);
  const [moveConflict, setMoveConflict] = useState<{
    fixture: LightingChannel;
    current: LightingChannel;
    desiredBarId: number;
    desiredPosition: number;
  } | null>(null);

  const moveFixture = useMoveFixture();
  const reorderBar = useReorderBar();
  const deleteFixture = useDeleteFixture();
  const testFixture = useTestFixture();
  const createGroup = useCreateGroup();
  const createBar = useCreateBar();
  const setLevels = useSetLevels();
  const editingReferences = useFixtureReferences(fixtureSheet?.fixture?.id ?? null);
  const deleteReferences = useFixtureReferences(deleteTarget?.id ?? null);
  const fixtureSave = useFixtureSave(() => setFixtureSheet(null));

  useEffect(
    () => () => {
      for (const timer of announceTimers.current) clearTimeout(timer);
    },
    [],
  );

  // Keyboard-driven focus moves only when focus already lives inside the
  // plan — a state update must never yank focus away from something else on
  // the page (§21.12's roving tabIndex is about Tab order, not a focus trap).
  useEffect(() => {
    if (rovingId === null) return;
    const container = svgRef.current;
    if (!container || !container.contains(document.activeElement)) return;
    const el = container.querySelector<HTMLElement>(`[data-fixture-id="${rovingId}"]`);
    if (el && el !== document.activeElement) el.focus();
  }, [rovingId]);

  function handleTap(id: number): void {
    setFocusedId(id);
    if (isAdmin) {
      setSelected((previous) => {
        const next = new Set(previous);
        if (next.has(id)) next.delete(id);
        else next.add(id);
        return next;
      });
    } else {
      // Operators never multi-select (§21.12): a tap always leaves at most one selected.
      setSelected((previous) => (previous.has(id) && previous.size === 1 ? new Set() : new Set([id])));
    }
  }

  function handleDoubleTap(id: number): void {
    setFocusedId(id);
    setFaderChannelId(id);
  }

  function handleLongPress(id: number, clientX: number, clientY: number): void {
    if (!editingEnabled) return;
    setContextMenu({ id, x: clientX, y: clientY });
  }

  function handleKeyDown(event: ReactKeyboardEvent<SVGGElement>, id: number): void {
    const key = event.key;
    if (key !== "ArrowLeft" && key !== "ArrowRight" && key !== "ArrowUp" && key !== "ArrowDown") return;
    event.preventDefault();
    const current = fixturesById.get(id);
    if (!current) return;
    const rovingFixtures: RovingFixture[] = placedFixtures.map((fixture) => ({
      id: fixture.id,
      bar_id: fixture.bar_id,
      position: fixture.position,
    }));
    const anchor = key === "ArrowLeft" || key === "ArrowRight" ? current.position : (anchorPosition.current ?? current.position);
    const target = moveRoving(rovingFixtures, orderedBarsList, id, key, anchor);
    if (!target) return;
    if (key === "ArrowLeft" || key === "ArrowRight") anchorPosition.current = target.position;
    setFocusedId(target.id);
  }

  function clientToSvg(clientX: number, clientY: number): { x: number; y: number } {
    const rect = svgRef.current?.getBoundingClientRect();
    if (!rect || rect.width === 0 || rect.height === 0) return { x: 0, y: 0 };
    return {
      x: ((clientX - rect.left) / rect.width) * VIEW_WIDTH,
      y: ((clientY - rect.top) / rect.height) * viewHeight(orderedBarsList.length),
    };
  }

  function handleDragStart(id: number): void {
    const fixture = fixturesById.get(id);
    if (!fixture) return;
    const barIndex = barIndexById.get(fixture.bar_id) ?? 0;
    setDragState({ id, x: fixtureX(fixture.position), y: barY(barIndex, orderedBarsList.length) });
  }

  function handleDragMove(id: number, clientX: number, clientY: number): void {
    setDragState({ id, ...clientToSvg(clientX, clientY) });
  }

  function handleDragEnd(id: number, clientX: number, clientY: number): void {
    setDragState(null);
    const fixture = fixturesById.get(id);
    if (!fixture) return;
    const { x, y } = clientToSvg(clientX, clientY);
    const targetBar = orderedBarsList[barIndexFromY(y, orderedBarsList.length)];
    if (!targetBar) return;
    const position = positionFromX(x);
    moveFixture.mutate(
      { id, bar_id: targetBar.id, position, version: fixture.updated_at },
      {
        onError: (error) => {
          if (error instanceof ApiError && error.code === "conflict") {
            const current = error.detail["current"] as LightingChannel | undefined;
            if (current) {
              setMoveConflict({ fixture, current, desiredBarId: targetBar.id, desiredPosition: position });
              return;
            }
          }
          presentError(error);
        },
      },
    );
  }

  function barName(barId: number | null): string {
    if (barId === null) return "unassigned";
    return bars.find((bar) => bar.id === barId)?.name ?? "unknown bar";
  }

  function scheduleFadeAnnouncement(ids: readonly number[], levelsById: ReadonlyMap<number, number>, ms: number): void {
    const timer = setTimeout(() => {
      if (ids.length === 1) {
        const id = ids[0];
        const name = (id !== undefined ? fixturesById.get(id)?.name : undefined) ?? "Fixture";
        const level = Math.round((id !== undefined ? levelsById.get(id) : undefined) ?? 0);
        setAnnouncement(`${name} faded to ${level} percent.`);
      } else {
        setAnnouncement(`${ids.length} fixtures faded.`);
      }
    }, ms);
    announceTimers.current.push(timer);
  }

  function handleSetLevelApply(targetLevel: number): void {
    const ids = setLevelTargets.settable.map((fixture) => fixture.id);
    if (ids.length === 0) return;
    const currentLevels = new Map(ids.map((id) => [id, getDisplayLevel(id) ?? 0]));
    const nextLevels = proportionalLevels(currentLevels, targetLevel);
    const levels: Record<string, number> = {};
    for (const [id, level] of nextLevels) levels[String(id)] = level;
    const usedFadeMs = fadeMs();
    setLevels.mutate(
      { levels, fade_ms: usedFadeMs },
      {
        onSuccess: () => {
          setSetLevelOpen(false);
          scheduleFadeAnnouncement(ids, nextLevels, usedFadeMs);
        },
        onError: (error) => presentError(error),
      },
    );
  }

  function moveBar(index: number, direction: -1 | 1): void {
    const bar = orderedBarsList[index];
    const neighbour = orderedBarsList[index + direction];
    if (!bar || !neighbour) return;
    reorderBar.mutate({ id: bar.id, sort_order: neighbour.sort_order, version: bar.updated_at }, { onError: (error) => presentError(error) });
    reorderBar.mutate(
      { id: neighbour.id, sort_order: bar.sort_order, version: neighbour.updated_at },
      { onError: (error) => presentError(error) },
    );
  }

  function runTest(id: number, mode: FixtureTestMode): void {
    testFixture.mutate({ id, mode }, { onError: (error) => presentError(error) });
  }

  // §21.11: under external control the stage plan is read-only and house
  // lighting is unaffected, so Set level applies to the selection's house
  // dimmers only; its stage fixtures are skipped, and the controls say so.
  const setLevelTargets = setLevelTargetsUnderExternalControl(
    [...selected].flatMap((id) => fixturesById.get(id) ?? []),
    externalControl,
  );
  const selectedLevels = new Map(setLevelTargets.settable.map((fixture) => [fixture.id, getDisplayLevel(fixture.id) ?? 0]));
  const setLevelInitial = Math.max(0, ...[...selectedLevels.values(), 0]);

  const faderFixture = faderChannelId !== null ? fixturesById.get(faderChannelId) : undefined;
  const contextFixture = contextMenu ? fixturesById.get(contextMenu.id) : undefined;

  return (
    <div className="stage-plan-root">
      {isAdmin ? (
        <div className="stage-plan-admin-toolbar">
          <Button variant="secondary" disabled={!editingEnabled} onClick={() => setAddBarOpen(true)}>
            + Add bar
          </Button>
          <Button
            variant={editMode ? "primary" : "secondary"}
            helpId={editMode ? "lighting.stageplan.edit-mode" : undefined}
            aria-pressed={editMode}
            onClick={() => setEditMode((current) => !current)}
          >
            Edit mode {editMode ? "●" : "○"}
          </Button>
          <Button variant="secondary" onClick={() => setChannelMapOpen(true)}>
            Channel map
          </Button>
          {editingEnabled && orderedBarsList.length > 1 ? (
            <ul className="stage-plan-bar-reorder-list">
              {orderedBarsList.map((bar, index) => (
                <BarReorderRow
                  key={bar.id}
                  bar={bar}
                  canMoveUp={index < orderedBarsList.length - 1}
                  canMoveDown={index > 0}
                  onMoveUp={() => moveBar(index, 1)}
                  onMoveDown={() => moveBar(index, -1)}
                  onAddFixture={() => {
                    setFixtureSheetSeq((seq) => seq + 1);
                    setFixtureSheet({ fixture: null, barId: bar.id });
                  }}
                  disabled={reorderBar.isPending}
                />
              ))}
            </ul>
          ) : null}
          {editingEnabled ? (
            <Button
              variant="secondary"
              onClick={() => {
                setFixtureSheetSeq((seq) => seq + 1);
                setFixtureSheet({ fixture: null, barId: null });
              }}
            >
              Add fixture
            </Button>
          ) : null}
        </div>
      ) : null}

      <div className="stage-plan-container">
        <svg
          ref={svgRef}
          role="application"
          aria-label="Stage lighting plan"
          viewBox={`0 0 ${VIEW_WIDTH} ${viewHeight(orderedBarsList.length)}`}
          className="stage-plan-svg"
          data-dragging={dragState !== null || undefined}
        >
          <defs>
            <filter id="fixture-node-bloom-filter" x="-100%" y="-100%" width="300%" height="300%">
              <feGaussianBlur stdDeviation="8" />
            </filter>
          </defs>
          {orderedBarsList.map((bar, index) => {
            const y = barY(index, orderedBarsList.length);
            return (
              <g key={bar.id}>
                <line x1={20} x2={VIEW_WIDTH - 20} y1={y} y2={y} className="stage-plan-bar-line" />
                <text x={20} y={y} dy={-10} className="stage-plan-bar-label">
                  {bar.name}
                </text>
              </g>
            );
          })}
          {placedFixtures.map((fixture) => {
            // The fixture being dragged stays the SAME DOM node throughout the
            // gesture — only its drawn position changes — because the pointer
            // that started the drag has captured this element (§10.3's pointer
            // mechanics precedent, `FaderStrip`); swapping in a separate "ghost"
            // element here would orphan that capture mid-drag and silently
            // drop the pointerup that ends it.
            const dragging = dragState && dragState.id === fixture.id;
            const barIndex = barIndexById.get(fixture.bar_id) ?? 0;
            const x = dragging ? dragState.x : fixtureX(fixture.position);
            const y = dragging ? dragState.y : barY(barIndex, orderedBarsList.length);
            return (
              <StageFixtureNode
                key={fixture.id}
                fixture={fixture}
                x={x}
                y={y}
                tabIndex={fixture.id === rovingId ? 0 : -1}
                selected={selected.has(fixture.id)}
                mode={mode}
                conflict={conflicts.has(fixture.id)}
                draggable={editingEnabled}
                dragGhost={Boolean(dragging)}
                onTap={handleTap}
                onDoubleTap={handleDoubleTap}
                onLongPress={handleLongPress}
                onDragStart={handleDragStart}
                onDragMove={handleDragMove}
                onDragEnd={handleDragEnd}
                onFocus={setFocusedId}
                onKeyDown={handleKeyDown}
              />
            );
          })}
        </svg>
      </div>

      <div aria-live="polite" className="sr-only">
        {announcement}
      </div>

      {isAdmin ? (
        <SelectionBar
          count={selected.size}
          onGroup={() => {
            setGroupOpenSeq((seq) => seq + 1);
            setGroupOpen(true);
          }}
          onSetLevel={() => {
            setSetLevelOpenSeq((seq) => seq + 1);
            setSetLevelOpen(true);
          }}
          onClear={() => setSelected(new Set())}
          setLevelUnavailable={
            setLevelTargets.settable.length === 0 && setLevelTargets.skipped.length > 0
              ? "Stage fixtures are read-only under external control"
              : undefined
          }
        />
      ) : null}

      <Sheet
        open={faderChannelId !== null}
        onOpenChange={(open) => {
          if (!open) setFaderChannelId(null);
        }}
      >
        <SheetContent title={faderFixture?.name ?? "Fixture"}>{faderFixture ? <ChannelFader channel={faderFixture} className="stage-plan-fader" /> : null}</SheetContent>
      </Sheet>

      {isAdmin ? (
        <>
          {fixtureSheet ? (
            <FixtureSheet
              key={`fixture-${fixtureSheetSeq}`}
              open
              onOpenChange={(open) => {
                if (!open) setFixtureSheet(null);
              }}
              fixture={fixtureSheet.fixture}
              bars={orderedBarsList}
              profiles={profiles}
              devices={devices}
              knxAddresses={knxAddresses}
              channels={fixtures}
              groups={groups}
              initialBarId={fixtureSheet.barId}
              saving={fixtureSave.saving}
              onSave={(input) => fixtureSave.save(fixtureSheet.fixture, input)}
              fieldErrors={fixtureSave.fieldErrors}
              deleting={deleteFixture.isPending}
              references={editingReferences.data?.references ?? []}
              referencesLoading={editingReferences.isPending}
              onDelete={
                fixtureSheet.fixture
                  ? () => {
                      const target = fixtureSheet.fixture;
                      if (!target) return;
                      deleteFixture.mutate(target.id, {
                        onSuccess: () => setFixtureSheet(null),
                        onError: (error) => presentError(error),
                      });
                    }
                  : undefined
              }
            />
          ) : null}
          {fixtureSave.conflict ? (
            <ConflictDialog
              open
              onOpenChange={(open) => {
                if (!open) fixtureSave.reloadConflict();
              }}
              deviceName={fixtureSheet?.fixture?.name ?? "This fixture"}
              rows={[...fixtureSave.conflict.rows]}
              onReload={fixtureSave.reloadConflict}
              onOverwrite={fixtureSave.overwriteConflict}
            />
          ) : null}
          <AddBarDialog
            open={addBarOpen}
            onOpenChange={setAddBarOpen}
            adding={createBar.isPending}
            onAdd={(name) => {
              const nextOrder = orderedBarsList.length > 0 ? Math.max(...orderedBarsList.map((b) => b.sort_order)) + 1 : 0;
              createBar.mutate(
                { name, sort_order: nextOrder, notes: null },
                { onSuccess: () => setAddBarOpen(false), onError: (error) => presentError(error) },
              );
            }}
          />
          <Sheet open={channelMapOpen} onOpenChange={setChannelMapOpen}>
            <SheetContent title="DMX channel map" side="right">
              <ChannelMap channels={fixtures} profiles={profiles} devices={devices} />
            </SheetContent>
          </Sheet>
          {moveToBarTarget ? (
            <MoveToBarDialog
              open
              onOpenChange={(open) => {
                if (!open) setMoveToBarTarget(null);
              }}
              fixtureName={moveToBarTarget.name}
              bars={orderedBarsList}
              currentBarId={moveToBarTarget.bar_id}
              moving={moveFixture.isPending}
              onMove={(barId) => {
                const target = moveToBarTarget;
                setMoveToBarTarget(null);
                if (!target) return;
                moveFixture.mutate(
                  { id: target.id, bar_id: barId, position: target.position ?? 0.5, version: target.updated_at },
                  { onError: (error) => presentError(error) },
                );
              }}
            />
          ) : null}
          {deleteTarget ? (
            <FixtureDeleteDialog
              open
              onOpenChange={(open) => {
                if (!open) setDeleteTarget(null);
              }}
              fixtureName={deleteTarget.name}
              groupNames={groups.filter((g) => deleteTarget.group_ids.includes(g.id)).map((g) => g.name)}
              references={deleteReferences.data?.references ?? []}
              loading={deleteReferences.isPending}
              deleting={deleteFixture.isPending}
              onConfirm={() => {
                const target = deleteTarget;
                if (!target) return;
                deleteFixture.mutate(target.id, {
                  onSuccess: () => setDeleteTarget(null),
                  onError: (error) => presentError(error),
                });
              }}
            />
          ) : null}
          <GroupSheet
            key={`group-${groupOpenSeq}`}
            open={groupOpen}
            onOpenChange={setGroupOpen}
            count={selected.size}
            creating={createGroup.isPending}
            onCreate={(name) =>
              createGroup.mutate(
                { name, channel_ids: [...selected] },
                {
                  onSuccess: () => {
                    setGroupOpen(false);
                    setSelected(new Set());
                  },
                  onError: (error) => presentError(error),
                },
              )
            }
          />
          <SetLevelSheet
            key={`set-level-${setLevelOpenSeq}`}
            open={setLevelOpen}
            onOpenChange={setSetLevelOpen}
            count={setLevelTargets.settable.length}
            skipped={setLevelTargets.skipped.length}
            initialLevel={setLevelInitial}
            applying={setLevels.isPending}
            onApply={handleSetLevelApply}
          />
          {contextMenu && contextFixture ? (
            <FixtureContextMenu
              x={contextMenu.x}
              y={contextMenu.y}
              onOpenFader={() => {
                setFaderChannelId(contextMenu.id);
                setContextMenu(null);
              }}
              onEditFixture={() => {
                setFixtureSheetSeq((seq) => seq + 1);
                setFixtureSheet({ fixture: contextFixture, barId: null });
                setContextMenu(null);
              }}
              onMoveToBar={() => {
                setMoveToBarTarget(contextFixture);
                setContextMenu(null);
              }}
              onRemoveFromBar={() => {
                moveFixture.mutate({ id: contextFixture.id, bar_id: null, position: null, version: contextFixture.updated_at });
                setContextMenu(null);
              }}
              onTestFull={() => {
                runTest(contextFixture.id, "full");
                setContextMenu(null);
              }}
              onTestOff={() => {
                runTest(contextFixture.id, "off");
                setContextMenu(null);
              }}
              onDelete={() => {
                setDeleteTarget(contextFixture);
                setContextMenu(null);
              }}
              onClose={() => setContextMenu(null)}
            />
          ) : null}
          {moveConflict ? (
            <MoveConflictDialog
              open
              onOpenChange={(open) => {
                if (!open) setMoveConflict(null);
              }}
              fixtureName={moveConflict.fixture.name}
              currentBarName={barName(moveConflict.current.bar_id)}
              currentPosition={moveConflict.current.position}
              mineBarName={barName(moveConflict.desiredBarId)}
              minePosition={moveConflict.desiredPosition}
              onReload={() => setMoveConflict(null)}
              onOverwrite={() => {
                const target = moveConflict;
                setMoveConflict(null);
                if (!target) return;
                moveFixture.mutate({
                  id: target.fixture.id,
                  bar_id: target.desiredBarId,
                  position: target.desiredPosition,
                  version: target.current.updated_at,
                });
              }}
            />
          ) : null}
        </>
      ) : null}
    </div>
  );
}
