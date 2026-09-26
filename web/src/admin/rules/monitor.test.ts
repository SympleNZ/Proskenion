/*
 * The derived-status live monitor (spec §8.10): updates from the
 * server-sent events stream, and closes the connection the moment it is
 * disabled — this is what "the screen must not leak connections as tabs are
 * switched" means in practice.
 */
import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { useDerivedStatusMonitor } from "./monitor";

type Listener = (event: unknown) => void;

class MockEventSource {
  static instances: MockEventSource[] = [];
  url: string;
  closed = false;
  listeners = new Map<string, Set<Listener>>();

  constructor(url: string) {
    this.url = url;
    MockEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: Listener) {
    const set = this.listeners.get(type) ?? new Set();
    set.add(listener);
    this.listeners.set(type, set);
  }

  removeEventListener(type: string, listener: Listener) {
    this.listeners.get(type)?.delete(listener);
  }

  close() {
    this.closed = true;
  }

  emit(type: string, event: unknown) {
    for (const listener of this.listeners.get(type) ?? []) listener(event);
  }
}

const originalEventSource = globalThis.EventSource;

function currentSource(): MockEventSource {
  const source = MockEventSource.instances.at(-1);
  if (!source) throw new Error("no EventSource was constructed");
  return source;
}

describe("useDerivedStatusMonitor", () => {
  afterEach(() => {
    MockEventSource.instances = [];
    globalThis.EventSource = originalEventSource;
  });

  it("connects only while enabled, and applies each status event as it arrives", () => {
    globalThis.EventSource = MockEventSource as unknown as typeof EventSource;
    const { result, rerender } = renderHook(({ enabled }: { enabled: boolean }) => useDerivedStatusMonitor(enabled), {
      initialProps: { enabled: false },
    });

    expect(result.current.connection).toBe("idle");
    expect(MockEventSource.instances).toHaveLength(0);

    rerender({ enabled: true });
    expect(MockEventSource.instances).toHaveLength(1);

    act(() => currentSource().emit("open", {}));
    expect(result.current.connection).toBe("open");

    act(() =>
      currentSource().emit("status", {
        data: JSON.stringify({
          id: 1,
          name: "Bank 1 state",
          group_address: "1/0/11",
          source_type: "lighting_group_all_at",
          enabled: true,
          value: true,
          written: true,
          changed_at: "2026-09-11T08:00:00+12:00",
          held: false,
        }),
      }),
    );
    expect(result.current.readings.get(1)?.value).toBe(true);
    expect(result.current.readings.get(1)?.changed_at).toBe("2026-09-11T08:00:00+12:00");

    // A later reading for the same id replaces it rather than accumulating.
    act(() =>
      currentSource().emit("status", {
        data: JSON.stringify({
          id: 1,
          name: "Bank 1 state",
          group_address: "1/0/11",
          source_type: "lighting_group_all_at",
          enabled: true,
          value: false,
          written: false,
          changed_at: "2026-09-11T08:05:00+12:00",
          held: false,
        }),
      }),
    );
    expect(result.current.readings.get(1)?.value).toBe(false);
    expect(result.current.readings.size).toBe(1);
  });

  it("closes the connection the instant it is disabled", () => {
    globalThis.EventSource = MockEventSource as unknown as typeof EventSource;
    const { rerender } = renderHook(({ enabled }: { enabled: boolean }) => useDerivedStatusMonitor(enabled), {
      initialProps: { enabled: true },
    });
    const source = currentSource();
    expect(source.closed).toBe(false);

    rerender({ enabled: false });
    expect(source.closed).toBe(true);
  });

  it("closes the connection on unmount — switching away from the tab leaks nothing", () => {
    globalThis.EventSource = MockEventSource as unknown as typeof EventSource;
    const { unmount } = renderHook(() => useDerivedStatusMonitor(true));
    const source = currentSource();
    expect(source.closed).toBe(false);

    unmount();
    expect(source.closed).toBe(true);
  });

  it("never opens a second connection while the first is still enabled", () => {
    globalThis.EventSource = MockEventSource as unknown as typeof EventSource;
    const { rerender } = renderHook(({ enabled }: { enabled: boolean }) => useDerivedStatusMonitor(enabled), {
      initialProps: { enabled: true },
    });
    rerender({ enabled: true });
    expect(MockEventSource.instances).toHaveLength(1);
  });
});
