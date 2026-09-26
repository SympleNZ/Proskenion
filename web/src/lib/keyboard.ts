/*
 * Shared keyboard-shortcut handling (spec §24.2): "Ctrl/Cmd+S — Save the
 * current form" applies to every admin form, not one screen. Before this
 * file existed only the Devices screen wired it, inline in DeviceCard; every
 * other form's Save button was reachable only by pointer or by tabbing to it.
 */
import type { KeyboardEvent, KeyboardEventHandler } from "react";

function isSaveChord(event: KeyboardEvent): boolean {
  return (event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s";
}

/**
 * Attach to a `<form onSubmit={...}>`'s own `onKeyDown`. Ctrl/Cmd+S submits
 * the form exactly as its own submit button would — the same validation, the
 * same handler — via `requestSubmit()` rather than duplicating each form's
 * save logic here.
 */
export function saveFormOnShortcut(event: KeyboardEvent<HTMLFormElement>): void {
  if (!isSaveChord(event)) return;
  event.preventDefault();
  event.currentTarget.requestSubmit();
}

/**
 * For a screen whose save action isn't a `<form>` submit (a card or view
 * with its own Save button and handler). Returns an `onKeyDown` handler to
 * attach to the screen's outer element; `onSave` should be the same function
 * the Save button's `onClick` calls.
 */
export function saveOnShortcut(onSave: () => void): KeyboardEventHandler<HTMLElement> {
  return (event) => {
    if (!isSaveChord(event)) return;
    event.preventDefault();
    onSave();
  };
}
