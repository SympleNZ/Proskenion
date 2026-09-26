/*
 * Error code → presentation (spec §16.1 client behaviour column, §21.27).
 * One mapping for REST responses and WebSocket nacks. The important case is
 * value_out_of_range: the value was accepted, just not as sent, so it is a
 * success with a clamp and never a failure.
 *
 * Error toasts auto-dismiss at 30 s and are evictable like every other
 * variant (§21.26, B37). An earlier revision gave them no auto-dismiss and
 * made them ineligible for eviction: three offline devices then produced
 * three permanent toasts, after which no toast of any kind could appear —
 * including the success toast confirming the reconnection that fixed it.
 * The fix is this one constant, used everywhere an error toast is raised
 * below, rather than the `duration: Infinity` every call site used to pass.
 */
import { toast } from "sonner";

import { ApiError, NetworkError, type ApiErrorCode, type ErrorDetail } from "./client";

/** §21.26's error row: 30 s is long enough to read, and the banner carries anything that lasts longer. */
export const ERROR_TOAST_DURATION_MS = 30_000;

export type ErrorPresentation =
  /** unauthenticated → re-authentication overlay (§6.5); the session layer owns it */
  | { kind: "overlay"; code: "unauthenticated"; message: string; reason: string | undefined }
  /** permission_denied, not_found → show and stop; no retry */
  | { kind: "toast"; code: "permission_denied" | "not_found"; message: string; tone: "danger" | "warning" }
  /** conflict → reload-or-overwrite dialog with a diff from detail */
  | { kind: "dialog"; code: "conflict"; message: string; detail: ErrorDetail }
  /** in_use → show the reference list (§21.20) */
  | { kind: "references"; code: "in_use"; message: string; references: readonly unknown[] }
  /** validation_failed → field-level errors from detail.fields */
  | { kind: "inline"; code: "validation_failed"; message: string; fields: Readonly<Record<string, string>> }
  /** value_out_of_range → success: clamp to detail.clamped, brief amber pulse */
  | { kind: "clamp"; code: "value_out_of_range"; success: true; message: string; clamped: unknown }
  /** device_unavailable → toast with a retry action */
  | { kind: "retry"; code: "device_unavailable"; message: string }
  /** rate_limited → inline countdown on the control from detail.retry_after */
  | { kind: "countdown"; code: "rate_limited"; message: string; retryAfter: number }
  /** internal_error → show request_id, offer to copy it */
  | { kind: "copyable"; code: "internal_error"; message: string; requestId: string | undefined };

export type PresentationKind = ErrorPresentation["kind"];

/** Which presentation each code in the closed vocabulary gets. */
export const PRESENTATION_BY_CODE: Readonly<Record<ApiErrorCode, PresentationKind>> = {
  unauthenticated: "overlay",
  permission_denied: "toast",
  not_found: "toast",
  conflict: "dialog",
  in_use: "references",
  validation_failed: "inline",
  value_out_of_range: "clamp",
  device_unavailable: "retry",
  rate_limited: "countdown",
  internal_error: "copyable",
};

/*
 * Two shapes reach us, because two producers write them. The wizard sends
 * `detail.fields` as a list of {field, message, type} (§16.4); the device
 * schema validator sends the map itself, keyed by dotted field path with a
 * list of messages (§5.5 `as_detail`). Both are field errors, so both are
 * read here rather than at each call site.
 */
const NOT_A_FIELD = new Set(["reason", "clamped", "retry_after", "references", "current", "device", "reverted"]);

function messageOf(value: unknown): string {
  if (typeof value === "string") return value;
  if (Array.isArray(value)) return value.map((v) => (typeof v === "string" ? v : String(v))).join(" ");
  return String(value);
}

function fieldErrors(detail: ErrorDetail): Record<string, string> {
  const named = detail["fields"] ?? detail["errors"];
  const raw = named ?? detail;
  const out: Record<string, string> = {};
  if (raw && typeof raw === "object" && !Array.isArray(raw)) {
    for (const [field, message] of Object.entries(raw as Record<string, unknown>)) {
      if (named === undefined && NOT_A_FIELD.has(field)) continue;
      out[field] = messageOf(message);
    }
  } else if (Array.isArray(raw)) {
    for (const entry of raw) {
      if (entry && typeof entry === "object") {
        const e = entry as { field?: unknown; message?: unknown };
        if (typeof e.field === "string") out[e.field] = typeof e.message === "string" ? e.message : "Invalid";
      }
    }
  }
  return out;
}

export function presentationFor(error: ApiError): ErrorPresentation {
  const { message, detail } = error;
  switch (error.code) {
    case "unauthenticated":
      return { kind: "overlay", code: error.code, message, reason: error.reason };
    case "permission_denied":
      return { kind: "toast", code: error.code, message, tone: "danger" };
    case "not_found":
      return { kind: "toast", code: error.code, message, tone: "warning" };
    case "conflict":
      return { kind: "dialog", code: error.code, message, detail };
    case "in_use": {
      const refs = detail["references"];
      return { kind: "references", code: error.code, message, references: Array.isArray(refs) ? refs : [] };
    }
    case "validation_failed":
      return { kind: "inline", code: error.code, message, fields: fieldErrors(detail) };
    case "value_out_of_range":
      return { kind: "clamp", code: error.code, success: true, message, clamped: detail["clamped"] };
    case "device_unavailable":
      return { kind: "retry", code: error.code, message };
    case "rate_limited":
      return { kind: "countdown", code: error.code, message, retryAfter: error.retryAfter ?? 0 };
    case "internal_error":
      return { kind: "copyable", code: error.code, message, requestId: error.requestId };
  }
}

/** A clamp is the one presentation that is not a failure. */
export function isFailure(presentation: ErrorPresentation): boolean {
  return presentation.kind !== "clamp";
}

export function copyRequestId(requestId: string): void {
  void navigator.clipboard?.writeText(requestId).catch(() => undefined);
}

export interface PresentOptions {
  /** Offered on device_unavailable toasts. */
  retry?: () => void;
}

/**
 * Show whatever can be shown globally (toasts) and hand the presentation back
 * so the caller can handle the inline kinds — countdown, clamp, field errors,
 * dialog. The session layer handles `overlay` through apiEvents, so this
 * never shows a second thing for it. Unknown errors become a generic toast.
 */
export function presentError(error: unknown, options: PresentOptions = {}): ErrorPresentation | undefined {
  if (error instanceof NetworkError) {
    toast.error(error.message, { duration: ERROR_TOAST_DURATION_MS });
    return undefined;
  }
  if (!(error instanceof ApiError)) {
    toast.error("Something went wrong", { duration: ERROR_TOAST_DURATION_MS });
    return undefined;
  }
  const presentation = presentationFor(error);
  switch (presentation.kind) {
    case "toast":
      if (presentation.tone === "danger") toast.error(presentation.message, { duration: ERROR_TOAST_DURATION_MS });
      else toast.warning(presentation.message);
      break;
    case "retry":
      toast.error(presentation.message, {
        duration: ERROR_TOAST_DURATION_MS,
        ...(options.retry ? { action: { label: "Retry", onClick: options.retry } } : {}),
      });
      break;
    case "copyable": {
      const id = presentation.requestId;
      toast.error(presentation.message, {
        duration: ERROR_TOAST_DURATION_MS,
        ...(id
          ? {
              description: `Request ${id}`,
              action: { label: "Copy ID", onClick: () => copyRequestId(id) },
            }
          : {}),
      });
      break;
    }
    default:
      break;
  }
  return presentation;
}
