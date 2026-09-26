/*
 * REST client (spec §16). One fetch wrapper, one error envelope, one closed
 * vocabulary of error codes. Every non-2xx response is parsed into an
 * ApiError; presentation is decided in errors.ts, never here.
 */

export const API_BASE = "/api/v1";

/** The closed vocabulary of §16.1. WebSocket nacks draw from the same list. */
export const API_ERROR_CODES = [
  "unauthenticated",
  "permission_denied",
  "not_found",
  "conflict",
  "in_use",
  "validation_failed",
  "value_out_of_range",
  "device_unavailable",
  "rate_limited",
  "internal_error",
] as const;

export type ApiErrorCode = (typeof API_ERROR_CODES)[number];

export type ErrorDetail = Record<string, unknown>;

export interface ErrorEnvelope {
  error: {
    code: string;
    message: string;
    detail?: ErrorDetail | null;
    request_id?: string;
  };
}

export function isApiErrorCode(code: string): code is ApiErrorCode {
  return (API_ERROR_CODES as readonly string[]).includes(code);
}

export class ApiError extends Error {
  readonly code: ApiErrorCode;
  readonly status: number;
  readonly detail: ErrorDetail;
  readonly requestId: string | undefined;

  constructor(status: number, code: ApiErrorCode, message: string, detail?: ErrorDetail | null, requestId?: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.detail = detail ?? {};
    this.requestId = requestId;
  }

  /** `detail.reason`, used by permission_denied and unauthenticated to say why. */
  get reason(): string | undefined {
    const r = this.detail["reason"];
    return typeof r === "string" ? r : undefined;
  }

  /** `detail.retry_after` in whole seconds, for rate_limited. */
  get retryAfter(): number | undefined {
    const r = this.detail["retry_after"];
    return typeof r === "number" && Number.isFinite(r) ? Math.max(0, Math.ceil(r)) : undefined;
  }

  static fromEnvelope(status: number, body: unknown): ApiError {
    const env = body as Partial<ErrorEnvelope> | null;
    const err = env?.error;
    if (err && typeof err.code === "string") {
      const code = isApiErrorCode(err.code) ? err.code : "internal_error";
      const message = typeof err.message === "string" && err.message ? err.message : defaultMessage(code);
      return new ApiError(status, code, message, err.detail ?? null, err.request_id);
    }
    // A response without the envelope: nginx, a crash, a proxy. Map by status.
    return new ApiError(status, codeForStatus(status), defaultMessage(codeForStatus(status)));
  }
}

/** The request never reached the server, or the reply never arrived. Not in the vocabulary. */
export class NetworkError extends Error {
  constructor(message = "Could not reach the controller") {
    super(message);
    this.name = "NetworkError";
  }
}

function codeForStatus(status: number): ApiErrorCode {
  switch (status) {
    case 401:
      return "unauthenticated";
    case 403:
      return "permission_denied";
    case 404:
      return "not_found";
    case 409:
      return "conflict";
    case 422:
      return "validation_failed";
    case 429:
      return "rate_limited";
    case 503:
      return "device_unavailable";
    default:
      return "internal_error";
  }
}

export function defaultMessage(code: ApiErrorCode): string {
  switch (code) {
    case "unauthenticated":
      return "Your session has ended";
    case "permission_denied":
      return "Not permitted";
    case "not_found":
      return "Not found";
    case "conflict":
      return "This was changed by someone else";
    case "in_use":
      return "Still in use";
    case "validation_failed":
      return "Check the highlighted fields";
    case "value_out_of_range":
      return "Value adjusted to the permitted range";
    case "device_unavailable":
      return "Device unavailable";
    case "rate_limited":
      return "Too many attempts";
    case "internal_error":
      return "The server returned an error";
  }
}

/*
 * Cross-cutting outcomes are announced on a tiny event target so the session
 * layer can react wherever the call originated: `first-run` (403
 * permission_denied with reason first_run_incomplete → /setup) and
 * `unauthenticated` (401 → overlay or /login, §6.5).
 */
export const apiEvents = new EventTarget();

export type ApiEventName = "first-run" | "unauthenticated";

export function onApiEvent(name: ApiEventName, handler: (error: ApiError) => void): () => void {
  const listener = (event: Event) => handler((event as CustomEvent<ApiError>).detail);
  apiEvents.addEventListener(name, listener);
  return () => apiEvents.removeEventListener(name, listener);
}

/**
 * A session that ended outside any request — the live socket's close codes
 * 4002 and 4003 (§16.8) — announced exactly as a 401 with `reason` would be,
 * so the session layer has one place that decides what the user sees.
 */
export function announceUnauthenticated(reason: string, message: string): void {
  announce(new ApiError(401, "unauthenticated", message, { reason }));
}

function announce(error: ApiError): void {
  if (error.code === "permission_denied" && error.reason === "first_run_incomplete") {
    apiEvents.dispatchEvent(new CustomEvent<ApiError>("first-run", { detail: error }));
  } else if (error.code === "unauthenticated") {
    apiEvents.dispatchEvent(new CustomEvent<ApiError>("unauthenticated", { detail: error }));
  }
}

export interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  body?: unknown;
  signal?: AbortSignal;
  /**
   * Skip the cross-cutting announcements. Login endpoints use this: a wrong
   * password is a 401 the form handles itself, not a session expiry.
   */
  quiet?: boolean;
  /** Absolute path instead of one under API_BASE, e.g. `/health`. */
  absolute?: boolean;
  /**
   * Extra request headers. `If-Unmodified-Since-Version` on a configuration
   * PUT is the one that matters: it carries the `updated_at` the client read,
   * so a concurrent edit answers 409 rather than being overwritten (§16.1).
   */
  headers?: Record<string, string>;
}

/**
 * Fetch `path` under /api/v1 and return the parsed JSON body. Throws ApiError
 * for any non-2xx response and NetworkError when the request fails outright.
 */
export async function api<T = void>(path: string, options: RequestOptions = {}): Promise<T> {
  const url = options.absolute ? path : `${API_BASE}${path}`;
  const headers: Record<string, string> = { Accept: "application/json" };
  const init: RequestInit = {
    method: options.method ?? (options.body === undefined ? "GET" : "POST"),
    credentials: "same-origin",
    headers,
  };
  if (options.headers) Object.assign(headers, options.headers);
  if (options.signal) init.signal = options.signal;
  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(options.body);
  }

  let response: Response;
  try {
    response = await fetch(url, init);
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === "AbortError") throw cause;
    throw new NetworkError();
  }

  if (response.status === 204) return undefined as T;

  let body: unknown = null;
  const text = await response.text();
  if (text) {
    try {
      body = JSON.parse(text) as unknown;
    } catch {
      body = null;
    }
  }

  if (!response.ok) {
    const error = ApiError.fromEnvelope(response.status, body);
    if (!options.quiet) announce(error);
    throw error;
  }
  return body as T;
}
