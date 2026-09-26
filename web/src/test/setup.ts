import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// jsdom has no ResizeObserver. Radix's Popover (the inline-help affordance,
// spec §19.1) measures its anchor with one even before it opens.
class ResizeObserverStub {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}
globalThis.ResizeObserver ??= ResizeObserverStub as unknown as typeof ResizeObserver;

// jsdom has no EventSource either. The KNX live monitor (spec §21.19) and the
// Rules/Derived status tabs' live state stream (spec §8.10, §21.17) each open
// one as soon as their panel mounts or expands, whether or not a test cares
// about the telegrams or state changes — this stub just means that never
// throws, for either module's own style (KNX sets onmessage/onerror
// directly; Rules uses addEventListener).
class EventSourceStub {
  onmessage: ((event: unknown) => void) | null = null;
  onerror: ((event: unknown) => void) | null = null;
  addEventListener(): void {}
  removeEventListener(): void {}
  close(): void {}
}
globalThis.EventSource ??= EventSourceStub as unknown as typeof EventSource;

afterEach(() => {
  cleanup();
});
