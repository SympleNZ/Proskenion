/*
 * The WebSocket client (spec §16.8, §10.7, §21.2). The transport half of the
 * live store: it owns the socket, the reconnection policy and the write
 * protocol, and hands every inbound frame to `applyMessage`.
 *
 * Nothing is replayed on reconnect — not a queued local write, not a discrete
 * event that fired during the outage (§10.7). Replaying a queued mute-off
 * after a 90-second outage could unmute a live microphone on an intent formed
 * a minute and a half earlier, in a room whose state has since moved on. The
 * resync snapshot tells the client where things actually are; event history
 * stays in the server-side logs.
 *
 * Pings, pongs, resyncs and broadcasts are never session activity (§6.4, B65).
 * Foreground presence holds a session and that is `useSessionHold`'s job
 * alone: nothing in this file imports anything from `@/session`, and a test
 * asserts it.
 */
import { ApiError, announceUnauthenticated, defaultMessage, isApiErrorCode, type ApiErrorCode } from "@/api/client";
import { presentationFor, type ErrorPresentation } from "@/api/errors";
import { notePing, setConnectionFault, setRetryHandler } from "@/live/connection";
import {
  abandonWrites,
  clearLamps,
  clearMeters,
  clearProgress,
  controlsEnabled,
  applyMessage,
  getConnectionState,
  resolveAck,
  resolveNack,
  setConnectionState,
  setPendingWrite,
  type WriteDomain,
  type WriteTarget,
} from "@/live/store";

/**
 * The numeric channel id a `set` frame carries on the wire (§16.8): always
 * the real id, even for a `MixerRef` targeting Main, whose *store* key is
 * normalised to a fixed internal id by `mixerKey` — the wire and the store
 * key deliberately part ways here (see `mixerKey`'s own comment).
 */
function wireId(id: WriteTarget | null): number | null {
  if (id === null) return null;
  return typeof id === "number" ? id : id.id;
}

/** `WS /ws?v=1`. An unknown or missing version is closed with 4001 (§16.8). */
export const WS_PATH = "/ws";
export const PROTOCOL_VERSION = "1";

/** Exponential backoff, from one second, capped at thirty (§10.7). */
export const BACKOFF_START_MS = 1_000;
export const BACKOFF_CAP_MS = 30_000;

/** The client's own keep-alive, against intermediaries closing an idle socket (§16.8). */
export const CLIENT_PING_INTERVAL_MS = 30_000;

/** Reconnection failing for this long shows the full-screen connection-lost state (§10.7). */
export const CONNECTION_LOST_AFTER_MS = 5 * 60_000;

export const CLOSE_REFRESH_REQUIRED = 4001;
/** The session reached its absolute (12-hour) expiry; any tier (§6.4). */
export const CLOSE_SESSION_EXPIRED = 4002;
/** Hirer access was disabled or the PIN changed (§6.6); hirers only. */
export const CLOSE_ACCESS_REVOKED = 4003;

/**
 * What each session-ending close code is announced as — the `detail.reason`
 * a REST 401 would carry. `absolute_expiry` sends staff to the login screen
 * and a hirer to the PIN screen; `hirer_revoked` is "Access updated", with
 * no login prompt (§21.8).
 */
export const SESSION_END_REASONS: Readonly<Record<number, { reason: string; message: string }>> = {
  [CLOSE_SESSION_EXPIRED]: { reason: "absolute_expiry", message: "The session has reached its 12-hour limit" },
  [CLOSE_ACCESS_REVOKED]: { reason: "hirer_revoked", message: "Hire guest access has been withdrawn" },
};
const CLOSE_NORMAL = 1000;
const SOCKET_OPEN = 1;

/** The domains a client may name in `subscribe` and `resync` (§16.8). */
export const DEFAULT_DOMAINS: readonly string[] = [
  "lighting",
  "mixer",
  "devices",
  "timer",
  "projector",
  "hdmi",
  "scenes",
  "system",
  // Panel lamps (phase-5-contracts.md) — staff receive every lamp;
  // a hirer's socket is narrowed to `lamp_ids` server-side, same as mixer
  // and lighting are narrowed to reachable channels.
  "status",
];

/** The parts of a WebSocket this client uses; a fake one in tests supplies the same. */
export interface SocketLike {
  readonly readyState: number;
  send(data: string): void;
  close(code?: number, reason?: string): void;
  onopen: ((event: unknown) => void) | null;
  onclose: ((event: { code: number; reason?: string }) => void) | null;
  onmessage: ((event: { data: unknown }) => void) | null;
  onerror: ((event: unknown) => void) | null;
}

export interface LiveSocketOptions {
  /** Defaults to `ws(s)://<host>/ws?v=1` from the page's own origin. */
  url?: string;
  domains?: readonly string[];
  createSocket?: (url: string) => SocketLike;
}

function defaultUrl(): string {
  if (typeof window === "undefined" || !window.location) return `${WS_PATH}?v=${PROTOCOL_VERSION}`;
  const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${scheme}//${window.location.host}${WS_PATH}?v=${PROTOCOL_VERSION}`;
}

function defaultCreateSocket(url: string): SocketLike {
  return new WebSocket(url) as unknown as SocketLike;
}

/**
 * A nack's presentation, through the one §16.1 mapping that serves both
 * transports. `value_out_of_range` is a success with a clamp, never a failure.
 */
export function nackPresentation(reason: ApiErrorCode, value?: unknown): ErrorPresentation {
  // A nack has no HTTP status; the code is what decides behaviour (B35).
  const detail = value === undefined ? {} : { clamped: value };
  return presentationFor(new ApiError(0, reason, defaultMessage(reason), detail));
}

export class LiveSocket {
  private readonly url: string;
  private readonly domains: readonly string[];
  private readonly create: (url: string) => SocketLike;

  private socket: SocketLike | null = null;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private lostTimer: ReturnType<typeof setTimeout> | null = null;
  private pingTimer: ReturnType<typeof setInterval> | null = null;
  private backoff = BACKOFF_START_MS;
  /** Client-monotonic, unique per connection (§16.8); reset on every open. */
  private token = 0;
  private stopped = true;
  private refreshRequired = false;

  constructor(options: LiveSocketOptions = {}) {
    this.url = options.url ?? defaultUrl();
    this.domains = options.domains ?? DEFAULT_DOMAINS;
    this.create = options.createSocket ?? defaultCreateSocket;
    this.onVisibility = this.onVisibility.bind(this);
  }

  start(): void {
    if (!this.stopped) return;
    this.stopped = false;
    this.refreshRequired = false;
    if (typeof document !== "undefined") {
      document.addEventListener("visibilitychange", this.onVisibility);
    }
    this.open();
  }

  stop(): void {
    this.stopped = true;
    this.clearTimer("reconnectTimer");
    this.clearTimer("lostTimer");
    this.stopPinging();
    if (typeof document !== "undefined") {
      document.removeEventListener("visibilitychange", this.onVisibility);
    }
    const socket = this.socket;
    this.socket = null;
    socket?.close(CLOSE_NORMAL);
  }

  /** The manual retry behind the connection-lost overlay (§10.7). */
  retry(): void {
    if (this.refreshRequired) return; // only a reload cures a version mismatch
    this.clearTimer("reconnectTimer");
    this.backoff = BACKOFF_START_MS;
    this.stopped = false;
    if (this.socket) return;
    this.open();
  }

  isOpen(): boolean {
    return this.socket !== null && this.socket.readyState === SOCKET_OPEN;
  }

  /** How long the next reconnection attempt will wait; diagnostics and tests. */
  get backoffMs(): number {
    return this.backoff;
  }

  /**
   * A continuous write (§21.2). Returns the token, or null when nothing was
   * sent: during an outage every control is disabled precisely so no write is
   * issued that would be lost, and there is deliberately no queue.
   *
   * `value` is in core units — lighting 0–100, mixer dB — never a position and
   * never a wire format (§5.5, B41). `null` is a mixer channel dragged to off;
   * lighting, the group and master domains never send it.
   */
  send(domain: WriteDomain, id: WriteTarget | null, value: number | null): number | null {
    if (!this.isOpen() || !controlsEnabled()) return null;
    const token = ++this.token;
    setPendingWrite(domain, id, value, token);
    this.post({ type: "set", domain, id: wireId(id), value, token });
    return token;
  }

  /**
   * Ask for a full snapshot per domain. Meters are never replayed (§16.8);
   * lamps are dropped the same way, because which lamp a connection may see
   * can shrink between one resync and the next (a page reassigned, a
   * hirer's access narrowed) and nothing should be left standing stale.
   * Progress is dropped for the same reason: a `progress` frame
   * missed across a drop — the operation finished while this socket was
   * down — must not leave a card reading "running" forever with no
   * completion event left to ever clear it. A still-running operation
   * simply reappears on its own next frame.
   */
  resync(): void {
    clearMeters();
    clearLamps();
    clearProgress();
    this.post({ type: "resync", domains: this.domains });
  }

  private open(): void {
    if (this.socket || this.stopped) return;
    let socket: SocketLike;
    try {
      socket = this.create(this.url);
    } catch {
      this.scheduleReconnect();
      return;
    }
    this.socket = socket;
    socket.onopen = () => this.onOpen();
    socket.onmessage = (event) => this.onMessage(event.data);
    socket.onclose = (event) => this.onClose(event?.code ?? CLOSE_NORMAL);
    // A socket error is always followed by a close; the close does the work.
    socket.onerror = () => undefined;
  }

  private onOpen(): void {
    this.backoff = BACKOFF_START_MS;
    this.token = 0;
    this.clearTimer("lostTimer");
    // Nothing in flight survives a reconnection (§10.7).
    abandonWrites();
    setConnectionState("connected");
    setConnectionFault(null);
    this.post({ type: "subscribe", domains: this.domains });
    this.resync();
    this.startPinging();
  }

  private onClose(code: number): void {
    this.stopPinging();
    this.socket = null;
    abandonWrites();
    const ended = SESSION_END_REASONS[code];
    if (ended) {
      // The session is over, not the connection: reconnecting would only be
      // refused at the upgrade. The session layer takes the whole screen — a
      // redirect or the "Access updated" dialog — so no reconnecting banner or
      // connection-lost overlay competes with it.
      this.stopped = true;
      this.clearTimer("reconnectTimer");
      this.clearTimer("lostTimer");
      announceUnauthenticated(ended.reason, ended.message);
      return;
    }
    if (code === CLOSE_REFRESH_REQUIRED) {
      // The build on screen no longer speaks the server's protocol. Retrying
      // would loop; the user has to reload (§16.8).
      this.refreshRequired = true;
      this.stopped = true;
      this.clearTimer("reconnectTimer");
      this.clearTimer("lostTimer");
      setConnectionFault("refresh_required");
      setConnectionState("lost");
      return;
    }
    if (this.stopped) return;
    // A further failed attempt does not take the connection-lost overlay back
    // down to a banner: five minutes of failure is still five minutes (§10.7).
    if (getConnectionState() !== "lost") setConnectionState("reconnecting");
    this.scheduleLost();
    this.scheduleReconnect();
  }

  private scheduleReconnect(): void {
    if (this.stopped || this.reconnectTimer !== null) return;
    const wait = this.backoff;
    this.backoff = Math.min(this.backoff * 2, BACKOFF_CAP_MS);
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.open();
    }, wait);
  }

  private scheduleLost(): void {
    if (this.lostTimer !== null) return;
    this.lostTimer = setTimeout(() => {
      this.lostTimer = null;
      // Still failing after five minutes: the server may have moved address,
      // or the tab has been backgrounded for a long time (§10.7). Attempts
      // continue behind the overlay at the backoff cap.
      setConnectionState("lost");
    }, CONNECTION_LOST_AFTER_MS);
  }

  private startPinging(): void {
    this.stopPinging();
    this.pingTimer = setInterval(() => this.post({ type: "ping" }), CLIENT_PING_INTERVAL_MS);
  }

  private stopPinging(): void {
    if (this.pingTimer === null) return;
    clearInterval(this.pingTimer);
    this.pingTimer = null;
  }

  private clearTimer(which: "reconnectTimer" | "lostTimer"): void {
    const timer = this[which];
    if (timer === null) return;
    clearTimeout(timer);
    this[which] = null;
  }

  private post(message: Record<string, unknown>): void {
    if (!this.isOpen()) return;
    try {
      this.socket?.send(JSON.stringify(message));
    } catch {
      // A socket that died between the readyState check and the send closes
      // by itself; there is nothing useful to do here.
    }
  }

  private onMessage(data: unknown): void {
    let message: unknown;
    try {
      message = JSON.parse(typeof data === "string" ? data : String(data));
    } catch {
      return;
    }
    if (!message || typeof message !== "object") return;
    const frame = message as Record<string, unknown>;
    switch (frame["type"]) {
      case "ping":
        // Liveness, not activity: the answer goes nowhere near the session.
        this.post({ type: "pong" });
        notePing();
        return;
      case "pong":
        notePing();
        return;
      case "ack": {
        const token = frame["token"];
        if (typeof token === "number") resolveAck(token);
        return;
      }
      case "nack": {
        const token = frame["token"];
        const raw = frame["reason"];
        if (typeof token !== "number") return;
        const reason: ApiErrorCode = typeof raw === "string" && isApiErrorCode(raw) ? raw : "internal_error";
        resolveNack(token, reason, frame["value"]);
        return;
      }
      default:
        applyMessage(message);
    }
  }

  private onVisibility(): void {
    if (typeof document === "undefined") return;
    if (document.visibilityState === "hidden") {
      // A suspended tablet still has meters composed and queued for it, and
      // metering is the most expensive thing on the wire (§16.8).
      this.post({ type: "background" });
      return;
    }
    if (this.isOpen()) this.resync();
    else this.retry();
  }
}

// -- the application's socket ---------------------------------------------------------

let current: LiveSocket | null = null;

export function startLiveSocket(options: LiveSocketOptions = {}): LiveSocket {
  if (!current) current = new LiveSocket(options);
  const socket = current;
  // The connection-lost overlay's manual retry moves the state back to
  // reconnecting; that is what reaches the socket (§10.7), so the component
  // needs to know nothing about it.
  setRetryHandler(() => socket.retry());
  socket.start();
  return socket;
}

export function stopLiveSocket(): void {
  setRetryHandler(null);
  current?.stop();
  current = null;
}

export function getLiveSocket(): LiveSocket | null {
  return current;
}

/** A continuous write over the open socket (§21.2). Null when nothing was sent. */
export function send(domain: WriteDomain, id: WriteTarget | null, value: number | null): number | null {
  return current?.send(domain, id, value) ?? null;
}
