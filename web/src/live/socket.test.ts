/*
 * The WebSocket client (spec §16.8, §10.7; acceptance in §22.3). Liveness,
 * reconnection, the write protocol and the one rule that is easy to undo by
 * being helpful: nothing is replayed.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { onApiEvent, type ApiError } from "@/api/client";
import { isFailure } from "@/api/errors";
import {
  getConnectionFault,
  getConnectionMessage,
  getLastPingAt,
  REFRESH_REQUIRED_MESSAGE,
  setRetryHandler,
} from "@/live/connection";
import {
  controlsEnabled,
  getConnectionState,
  getLamp,
  getLevel,
  getMeter,
  getMixer,
  getMixerDb,
  getProgress,
  hasPending,
  levelKey,
  resetLiveState,
  setLevel,
} from "@/live/store";

import {
  BACKOFF_CAP_MS,
  BACKOFF_START_MS,
  CLIENT_PING_INTERVAL_MS,
  CLOSE_ACCESS_REVOKED,
  CLOSE_REFRESH_REQUIRED,
  CLOSE_SESSION_EXPIRED,
  CONNECTION_LOST_AFTER_MS,
  LiveSocket,
  nackPresentation,
  type SocketLike,
} from "./socket";

class FakeSocket implements SocketLike {
  readyState = 0;
  readonly sent: string[] = [];
  onopen: ((event: unknown) => void) | null = null;
  onclose: ((event: { code: number; reason?: string }) => void) | null = null;
  onmessage: ((event: { data: unknown }) => void) | null = null;
  onerror: ((event: unknown) => void) | null = null;

  constructor(readonly url: string) {}

  send(data: string): void {
    this.sent.push(data);
  }

  close(code = 1000): void {
    this.readyState = 3;
    this.onclose?.({ code });
  }

  /** The server accepted the upgrade. */
  accept(): void {
    this.readyState = 1;
    this.onopen?.({});
  }

  receive(message: unknown): void {
    this.onmessage?.({ data: JSON.stringify(message) });
  }

  messages(): Record<string, unknown>[] {
    return this.sent.map((text) => JSON.parse(text) as Record<string, unknown>);
  }

  types(): unknown[] {
    return this.messages().map((message) => message["type"]);
  }
}

let sockets: FakeSocket[] = [];
let socket: LiveSocket;

function latest(): FakeSocket {
  const last = sockets[sockets.length - 1];
  if (!last) throw new Error("no socket was created");
  return last;
}

function connect(): FakeSocket {
  socket.start();
  const first = latest();
  first.accept();
  return first;
}

beforeEach(() => {
  vi.useFakeTimers();
  resetLiveState();
  sockets = [];
  socket = new LiveSocket({
    url: "ws://appliance.test/ws?v=1",
    createSocket: (url) => {
      const fake = new FakeSocket(url);
      sockets.push(fake);
      return fake;
    },
  });
});

afterEach(() => {
  socket.stop();
  setRetryHandler(null);
  vi.useRealTimers();
});

describe("subscribing", () => {
  it("opens ws://…/ws?v=1 and asks for a snapshot per domain", () => {
    const server = connect();
    expect(server.url).toBe("ws://appliance.test/ws?v=1");
    expect(server.types()).toEqual(["subscribe", "resync"]);
    expect(server.messages()[0]?.["domains"]).toContain("lighting");
    expect(getConnectionState()).toBe("connected");
    expect(controlsEnabled()).toBe(true);
  });
});

describe("liveness", () => {
  it("answers the server's ping with a pong", () => {
    const server = connect();
    server.receive({ type: "ping" });
    expect(server.types()).toContain("pong");
  });

  it("never counts a ping as session activity", () => {
    const fetched = vi.fn();
    vi.stubGlobal("fetch", fetched);
    const server = connect();
    server.receive({ type: "ping" });
    server.receive({ type: "pong" });
    vi.advanceTimersByTime(CLIENT_PING_INTERVAL_MS * 3);
    // The session hold is useSessionHold's job alone (§6.4, B65): nothing here
    // reaches it, so nothing was requested.
    expect(fetched).not.toHaveBeenCalled();
    expect(getLastPingAt()).not.toBeNull();
    vi.unstubAllGlobals();
  });

  it("imports nothing from the session layer", () => {
    const source = readFileSync(resolve(process.cwd(), "src/live/socket.ts"), "utf8");
    expect(source).not.toMatch(/from "@\/session/);
  });

  it("sends its own ping every thirty seconds against idle intermediaries", () => {
    const server = connect();
    vi.advanceTimersByTime(CLIENT_PING_INTERVAL_MS - 1);
    expect(server.types()).not.toContain("ping");
    vi.advanceTimersByTime(1);
    expect(server.types()).toContain("ping");
  });
});

describe("backgrounding", () => {
  function visibility(state: "visible" | "hidden"): void {
    Object.defineProperty(document, "visibilityState", { value: state, configurable: true });
    document.dispatchEvent(new Event("visibilitychange"));
  }

  afterEach(() => {
    Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true });
  });

  it("says when it goes away and resyncs on return", () => {
    const server = connect();
    visibility("hidden");
    expect(server.types()).toContain("background");
    const before = server.types().filter((type) => type === "resync").length;
    visibility("visible");
    expect(server.types().filter((type) => type === "resync").length).toBe(before + 1);
  });
});

describe("writes", () => {
  it("allocates a monotonic token and records the pending entry", () => {
    const server = connect();
    const first = socket.send("mixer", 3, -5.0);
    const second = socket.send("lighting", 7, 82.5);
    expect(first).toBe(1);
    expect(second).toBe(2);
    expect(server.messages().at(-2)).toEqual({ type: "set", domain: "mixer", id: 3, value: -5.0, token: 1 });
    expect(getMixerDb(3)).toBe(-5.0);
    expect(getLevel(7)).toBe(82.5);
  });

  it("addresses an output or Main by its real channel id on the wire, but keys the pending entry by section (P4-T7)", () => {
    const server = connect();
    // Main's wire id is its real channel_id (1 here); the store still holds
    // it under the fixed internal key mixerKey({section: "main", id: 0}).
    const mainToken = socket.send("mixer", { section: "main", id: 1 }, 0.0);
    expect(server.messages().at(-1)).toEqual({ type: "set", domain: "mixer", id: 1, value: 0.0, token: mainToken });
    expect(getMixer({ section: "main", id: 0 })?.db).toBe(0.0);

    const outputToken = socket.send("mixer", { section: "output", id: 2 }, -6.0);
    expect(server.messages().at(-1)).toEqual({ type: "set", domain: "mixer", id: 2, value: -6.0, token: outputToken });
    expect(getMixer({ section: "output", id: 2 })?.db).toBe(-6.0);
    expect(getMixer(2)).toBeNull(); // not the input section's bucket for the same numeric id
  });

  it("clears the pending entry on acknowledgement", () => {
    const server = connect();
    setLevel(7, 20);
    const token = socket.send("lighting", 7, 82.5) as number;
    server.receive({ type: "ack", token });
    expect(hasPending(levelKey(7))).toBe(false);
    expect(getLevel(7)).toBe(20);
  });

  it("settles on the value a nack carries rather than a stale one", () => {
    const server = connect();
    const token = socket.send("mixer", 3, -2.0) as number;
    server.receive({ type: "nack", token, reason: "value_out_of_range", value: -6.0 });
    expect(getMixerDb(3)).toBe(-6.0);
  });

  it("presents value_out_of_range as a success with a clamp", () => {
    const presentation = nackPresentation("value_out_of_range", -6.0);
    expect(presentation.kind).toBe("clamp");
    expect(isFailure(presentation)).toBe(false);
    expect(presentation).toMatchObject({ success: true, clamped: -6.0 });
    // Everything else in the closed vocabulary is a failure.
    expect(isFailure(nackPresentation("permission_denied"))).toBe(true);
    expect(isFailure(nackPresentation("not_found"))).toBe(true);
  });

  it("issues nothing while the connection is down", () => {
    const server = connect();
    server.close(1006);
    expect(controlsEnabled()).toBe(false);
    expect(socket.send("lighting", 7, 50)).toBeNull();
  });
});

describe("reconnection", () => {
  it("backs off from one second to a thirty second cap", () => {
    const server = connect();
    server.close(1006);
    expect(getConnectionState()).toBe("reconnecting");

    const waits = [BACKOFF_START_MS, 2_000, 4_000, 8_000, 16_000, BACKOFF_CAP_MS, BACKOFF_CAP_MS];
    for (const wait of waits) {
      const before = sockets.length;
      vi.advanceTimersByTime(wait - 1);
      expect(sockets.length, `no attempt before ${wait} ms`).toBe(before);
      vi.advanceTimersByTime(1);
      expect(sockets.length, `an attempt at ${wait} ms`).toBe(before + 1);
      latest().close(1006); // the attempt failed too
    }
  });

  it("resyncs on reconnect, replays nothing and shows no stale meters, lamps or progress", () => {
    const server = connect();
    server.receive({ type: "mixer_meters", channels: { "1": [-12.4] } });
    server.receive({ type: "status", lamps: { "4": { on: true, transitioning: false } } });
    // A `backup_run` (or any long operation) left mid-way — the final frame
    // (step 4 of 4) is exactly what this drop is about to lose.
    server.receive({ type: "progress", operation: "backup_run", step: 2, of: 4, message: "Running the backup job" });
    expect(getLamp(4)).toEqual({ on: true, transitioning: false });
    setLevel(7, 30);
    socket.send("lighting", 7, 90); // in flight when the connection drops
    expect(getLevel(7)).toBe(90);

    server.close(1006);
    // Replaying a queued mute-off after a 90-second outage could unmute a live
    // microphone on an intent formed a minute and a half earlier (§10.7).
    expect(hasPending(levelKey(7))).toBe(false);
    expect(getLevel(7)).toBe(30);

    vi.advanceTimersByTime(BACKOFF_START_MS);
    const reconnected = latest();
    reconnected.accept();

    expect(reconnected.types()).toEqual(["subscribe", "resync"]);
    expect(reconnected.types()).not.toContain("set");
    expect(getConnectionState()).toBe("connected");
    // Cleared immediately on resync, before any reply — this fake server
    // never answers, so nothing repopulates it (the real server would send
    // a fresh mixer_meters catch-up here; see broadcast.py).
    expect(getMeter(1)).toBeNull();
    // Which lamp a connection may see can shrink between resyncs (a page
    // reassigned, a hirer's access narrowed): dropped the same way.
    expect(getLamp(4)).toBeNull();
    // The operation may have finished while this socket was down, with no
    // completion frame left to ever arrive: a card reading `step < of`
    // must not be left "running" forever.
    expect(getProgress("backup_run")).toBeNull();
  });

  it("keeps the last-known state on screen during the outage", () => {
    const server = connect();
    server.receive({ type: "lighting_state", channels: { "3": { level: 64 } }, source: "fade" });
    server.close(1006);
    expect(getLevel(3)).toBe(64);
    expect(getConnectionState()).toBe("reconnecting");
  });

  it("shows the connection-lost state after five minutes of failure", () => {
    const server = connect();
    server.close(1006);
    vi.advanceTimersByTime(CONNECTION_LOST_AFTER_MS - 1);
    expect(getConnectionState()).toBe("reconnecting");
    vi.advanceTimersByTime(1);
    expect(getConnectionState()).toBe("lost");
    // Attempts continue behind the overlay, so a server that moved address is
    // found — and a further failure does not take the overlay back down.
    const before = sockets.length;
    latest().close(1006);
    expect(getConnectionState()).toBe("lost");
    vi.advanceTimersByTime(BACKOFF_CAP_MS);
    expect(sockets.length).toBeGreaterThan(before);
  });

  it("surfaces close code 4001 as the refresh message and stops retrying", () => {
    const server = connect();
    server.close(CLOSE_REFRESH_REQUIRED);
    expect(getConnectionState()).toBe("lost");
    expect(getConnectionFault()).toBe("refresh_required");
    expect(getConnectionMessage()).toBe(REFRESH_REQUIRED_MESSAGE);
    const before = sockets.length;
    vi.advanceTimersByTime(BACKOFF_CAP_MS * 4);
    expect(sockets.length).toBe(before); // only a reload cures a version mismatch
  });

  it("reconnects at once on a manual retry", () => {
    const server = connect();
    server.close(1006);
    const before = sockets.length;
    socket.retry();
    expect(sockets.length).toBe(before + 1);
  });
});

describe("a session ended by the server (§16.8, §21.8)", () => {
  let announced: ApiError[] = [];
  let off: () => void = () => undefined;

  beforeEach(() => {
    announced = [];
    off = onApiEvent("unauthenticated", (error) => announced.push(error));
  });

  afterEach(() => off());

  it.each([
    [CLOSE_ACCESS_REVOKED, "hirer_revoked"],
    [CLOSE_SESSION_EXPIRED, "absolute_expiry"],
  ])("close %i is announced as unauthenticated/%s and never retried", (code, reason) => {
    const server = connect();
    server.close(code);
    expect(announced).toHaveLength(1);
    expect(announced[0]?.code).toBe("unauthenticated");
    expect(announced[0]?.reason).toBe(reason);
    // Reconnecting would only be refused at the upgrade.
    const before = sockets.length;
    vi.advanceTimersByTime(CONNECTION_LOST_AFTER_MS + BACKOFF_CAP_MS);
    expect(sockets.length).toBe(before);
    // The session layer takes the screen: no connection-lost overlay, no refresh fault.
    expect(getConnectionState()).not.toBe("lost");
    expect(getConnectionFault()).toBeNull();
    expect(announced).toHaveLength(1);
  });

  it("abandons a write in flight when access is revoked", () => {
    const server = connect();
    setLevel(7, 20);
    expect(socket.send("lighting", 7, 82.5)).not.toBeNull();
    expect(hasPending(levelKey(7))).toBe(true);
    server.close(CLOSE_ACCESS_REVOKED);
    expect(hasPending(levelKey(7))).toBe(false);
    expect(socket.send("lighting", 7, 50)).toBeNull();
  });

  it("does not announce an ordinary outage", () => {
    const server = connect();
    server.close(1006);
    expect(announced).toEqual([]);
  });
});
