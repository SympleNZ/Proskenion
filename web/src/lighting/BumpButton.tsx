/*
 * A group strip's BUMP (owner decision 2026-10-01, "Option A"; the mock's
 * `.mu` button under a group strip in `docs/operator-views.html`): a
 * desk-standard flash. Held, the group's DMX members go to full — still
 * scaled by the master — and let go, they return to their own levels. No
 * fader moves and no level is stored; the button lights while it is held.
 *
 * It is a momentary control, so it is driven by press and release rather
 * than click:
 *
 *   pointer   pointerdown presses and captures the pointer; pointerup,
 *             pointercancel and lostpointercapture release. Each pointer is
 *             tracked on its own, so two fingers on two BUMPs both hold.
 *   keyboard  Space or Enter held is a held BUMP (keydown presses, keyup
 *             releases; auto-repeat is ignored).
 *   leaving   losing focus, the window losing focus, the page going hidden,
 *             the button becoming disabled and unmounting all release —
 *             whatever the server's own safety nets (`bump.ts`), the screen
 *             never shows a BUMP held that no finger is holding.
 *
 * The button carries `touch-action: none`: a press on it is a bump, never
 * the start of the row's sideways swipe (v0.1.15's touch arbitration is the
 * fader's; a momentary button has no direction to wait for).
 */
import { useCallback, useEffect, useId, useRef, useSyncExternalStore, type KeyboardEvent, type PointerEvent } from "react";

import { holdBump, isHolding, releaseBump, subscribeBumps } from "./bump";

export interface BumpButtonProps {
  groupId: number;
  /** The group's name, for the accessible name "Bump <group> to full". */
  label: string;
  /** No socket, or external control holds the group's stage fixtures. */
  disabled?: boolean;
}

const KEY_SOURCE = "key";

function isBumpKey(key: string): boolean {
  return key === " " || key === "Enter";
}

export function BumpButton({ groupId, label, disabled = false }: BumpButtonProps) {
  const holder = useId();
  /** What is holding this button down: pointer ids, and the keyboard. */
  const sources = useRef(new Set<string>());
  const groupRef = useRef(groupId);
  const held = useSyncExternalStore(
    subscribeBumps,
    () => isHolding(holder),
    () => false,
  );

  function press(source: string): void {
    if (disabled) return;
    const first = sources.current.size === 0;
    sources.current.add(source);
    if (first) holdBump(groupRef.current, holder);
  }

  function release(source: string): void {
    if (!sources.current.delete(source)) return;
    if (sources.current.size === 0) releaseBump(groupRef.current, holder);
  }

  const releaseAll = useCallback((): void => {
    if (sources.current.size === 0) return;
    sources.current.clear();
    releaseBump(groupRef.current, holder);
  }, [holder]);

  useEffect(() => {
    groupRef.current = groupId;
  }, [groupId]);

  // Disabled while held (the socket dropped, external control engaged): let go.
  useEffect(() => {
    if (disabled) releaseAll();
  }, [disabled, releaseAll]);

  // The hold was dropped from under us (the socket went): forget the fingers.
  useEffect(() => {
    if (!held) sources.current.clear();
  }, [held]);

  // Leaving the page, or unmounting, always lets go.
  useEffect(() => {
    const onBlur = () => releaseAll();
    const onVisibility = () => {
      if (document.visibilityState === "hidden") releaseAll();
    };
    window.addEventListener("blur", onBlur);
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      window.removeEventListener("blur", onBlur);
      document.removeEventListener("visibilitychange", onVisibility);
      releaseAll();
    };
  }, [releaseAll]);

  function handlePointerDown(event: PointerEvent<HTMLButtonElement>): void {
    if (disabled) return;
    if (event.pointerType === "mouse" && event.button !== 0) return;
    // No text selection, no emulated mouse events and no focus-driven scroll
    // from a press: this is a momentary key, not a click target.
    event.preventDefault();
    try {
      event.currentTarget.setPointerCapture(event.pointerId);
    } catch {
      // jsdom and older engines: the release still arrives on this button.
    }
    press(`pointer:${event.pointerId}`);
  }

  function handlePointerEnd(event: PointerEvent<HTMLButtonElement>): void {
    release(`pointer:${event.pointerId}`);
  }

  function handleKeyDown(event: KeyboardEvent<HTMLButtonElement>): void {
    if (!isBumpKey(event.key)) return;
    event.preventDefault(); // no click on keyup: the key *is* the hold
    if (!event.repeat) press(KEY_SOURCE);
  }

  function handleKeyUp(event: KeyboardEvent<HTMLButtonElement>): void {
    if (!isBumpKey(event.key)) return;
    event.preventDefault();
    release(KEY_SOURCE);
  }

  return (
    <button
      type="button"
      className="lighting-bump-button"
      aria-label={`Bump ${label} to full`}
      aria-pressed={held}
      disabled={disabled}
      data-testid={`group-bump-${groupId}`}
      onPointerDown={handlePointerDown}
      onPointerUp={handlePointerEnd}
      onPointerCancel={handlePointerEnd}
      onLostPointerCapture={handlePointerEnd}
      onKeyDown={handleKeyDown}
      onKeyUp={handleKeyUp}
      onBlur={() => releaseAll()}
      onContextMenu={(event) => event.preventDefault()}
    >
      Bump
    </button>
  );
}
