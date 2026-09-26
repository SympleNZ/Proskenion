/* Ctrl/Cmd+S save shortcut (spec §24.2) — the primitives every form-bearing screen shares. */
import { describe, expect, it, vi } from "vitest";

import { saveFormOnShortcut, saveOnShortcut } from "./keyboard";

function keyEvent(overrides: Record<string, unknown> = {}) {
  const currentTarget = { requestSubmit: vi.fn() };
  const event = {
    key: "s",
    ctrlKey: true,
    metaKey: false,
    preventDefault: vi.fn(),
    currentTarget,
    ...overrides,
  };
  return event as unknown as Parameters<typeof saveFormOnShortcut>[0] & { currentTarget: typeof currentTarget; preventDefault: () => void };
}

describe("saveFormOnShortcut", () => {
  it("submits the form on Ctrl+S", () => {
    const event = keyEvent();
    saveFormOnShortcut(event);
    expect(event.preventDefault).toHaveBeenCalledTimes(1);
    expect(event.currentTarget.requestSubmit).toHaveBeenCalledTimes(1);
  });

  it("submits the form on Cmd+S (metaKey)", () => {
    const event = keyEvent({ ctrlKey: false, metaKey: true });
    saveFormOnShortcut(event);
    expect(event.currentTarget.requestSubmit).toHaveBeenCalledTimes(1);
  });

  it("is case-insensitive on the key", () => {
    const event = keyEvent({ key: "S" });
    saveFormOnShortcut(event);
    expect(event.currentTarget.requestSubmit).toHaveBeenCalledTimes(1);
  });

  it("ignores S without a modifier", () => {
    const event = keyEvent({ ctrlKey: false, metaKey: false });
    saveFormOnShortcut(event);
    expect(event.preventDefault).not.toHaveBeenCalled();
    expect(event.currentTarget.requestSubmit).not.toHaveBeenCalled();
  });

  it("ignores a different key with a modifier held", () => {
    const event = keyEvent({ key: "a" });
    saveFormOnShortcut(event);
    expect(event.currentTarget.requestSubmit).not.toHaveBeenCalled();
  });
});

describe("saveOnShortcut", () => {
  it("calls the given save function on Ctrl+S", () => {
    const onSave = vi.fn();
    const handler = saveOnShortcut(onSave);
    const event = keyEvent();
    handler(event);
    expect(event.preventDefault).toHaveBeenCalledTimes(1);
    expect(onSave).toHaveBeenCalledTimes(1);
  });

  it("does not call save without the chord", () => {
    const onSave = vi.fn();
    const handler = saveOnShortcut(onSave);
    handler(keyEvent({ ctrlKey: false, metaKey: false }));
    expect(onSave).not.toHaveBeenCalled();
  });
});
