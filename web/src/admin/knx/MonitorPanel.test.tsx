/*
 * The live monitor's connection lifecycle (§21.19 *Live monitor*): renders
 * telegrams from a mocked SSE stream, stays bounded, and closes the moment
 * it is no longer visible — collapsed, or its tab left — with no leaked
 * connection.
 */
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "@/test/render";

import { MonitorPanel } from "./MonitorPanel";
import type { EventSourceLike } from "./monitor";
import type { MonitorEntry } from "./types";

const client = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

class FakeEventSource implements EventSourceLike {
  onmessage: ((event: { data: string }) => void) | null = null;
  onerror: ((event: unknown) => void) | null = null;
  closed = false;
  constructor(public url: string) {}
  close() {
    this.closed = true;
  }
  emit(entry: MonitorEntry) {
    this.onmessage?.({ data: JSON.stringify(entry) });
  }
}

function entry(groupAddress: string, timestamp: string): MonitorEntry {
  return { timestamp, direction: "incoming", group_address: groupAddress, dpt: "1.001", value: true, raw: "01", source_address: null };
}

describe("MonitorPanel", () => {
  beforeEach(() => {
    client.api.mockReset();
    client.api.mockResolvedValue([]);
  });

  function open(maxEntries?: number) {
    const sources: FakeEventSource[] = [];
    const createSource = (url: string) => {
      const source = new FakeEventSource(url);
      sources.push(source);
      return source;
    };
    const result = renderWithProviders(<MonitorPanel onAddToLibrary={vi.fn()} createSource={createSource} maxEntries={maxEntries} />);
    fireEvent.click(screen.getByRole("button", { name: /Live monitor/ }));
    return { ...result, sources };
  }

  it("opens no connection while collapsed", () => {
    const createSource = vi.fn((url: string) => new FakeEventSource(url));
    renderWithProviders(<MonitorPanel onAddToLibrary={vi.fn()} createSource={createSource} />);
    expect(screen.queryByLabelText("Filter by address")).not.toBeInTheDocument();
    expect(createSource).not.toHaveBeenCalled();
  });

  it("renders telegrams pushed by the mocked stream", async () => {
    const { sources } = open();
    await waitFor(() => expect(sources).toHaveLength(1));
    sources[0]?.emit(entry("1/0/1", "14:32:01.452"));
    expect(await screen.findByText("1/0/1")).toBeInTheDocument();
  });

  it("keeps only the most recent entries — a busy bus cannot grow the list without limit", async () => {
    const { sources } = open(3);
    await waitFor(() => expect(sources).toHaveLength(1));
    for (let i = 0; i < 5; i += 1) sources[0]?.emit(entry(`1/0/${i}`, `14:32:0${i}`));

    await screen.findByText("1/0/4");
    const rows = screen.getAllByRole("row").slice(1); // drop the header row
    expect(rows).toHaveLength(3);
    expect(screen.queryByText("1/0/0")).not.toBeInTheDocument();
  });

  it("closes the connection when the panel is collapsed again", async () => {
    const { sources } = open();
    await waitFor(() => expect(sources).toHaveLength(1));
    fireEvent.click(screen.getByRole("button", { name: /Live monitor/ }));
    expect(sources[0]?.closed).toBe(true);
  });

  it("closes the connection when the component is unmounted — leaving the tab", async () => {
    const { sources, unmount } = open();
    await waitFor(() => expect(sources).toHaveLength(1));
    unmount();
    expect(sources[0]?.closed).toBe(true);
  });
});
