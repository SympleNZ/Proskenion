/* Error code mapping (spec §16.1, §22.3): every code in the closed vocabulary maps to a defined presentation. */
import { describe, expect, it } from "vitest";

import { API_ERROR_CODES, ApiError, isApiErrorCode, type ApiErrorCode } from "./client";
import { PRESENTATION_BY_CODE, isFailure, presentationFor } from "./errors";

const make = (code: ApiErrorCode, detail: Record<string, unknown> = {}, requestId?: string) =>
  new ApiError(500, code, `message for ${code}`, detail, requestId);

describe("error code → presentation", () => {
  it("covers every code in the closed vocabulary", () => {
    expect(API_ERROR_CODES).toHaveLength(10);
    for (const code of API_ERROR_CODES) {
      expect(PRESENTATION_BY_CODE[code]).toBeTruthy();
      const presentation = presentationFor(make(code));
      expect(presentation.kind).toBe(PRESENTATION_BY_CODE[code]);
      expect(presentation.code).toBe(code);
      expect(presentation.message).toBe(`message for ${code}`);
    }
  });

  it("maps each code to the §16.1 client behaviour", () => {
    expect(PRESENTATION_BY_CODE).toEqual({
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
    });
  });

  it("presents value_out_of_range as success with a clamp, never a failure", () => {
    const presentation = presentationFor(make("value_out_of_range", { clamped: -12.5 }));
    expect(presentation.kind).toBe("clamp");
    if (presentation.kind !== "clamp") throw new Error("unreachable");
    expect(presentation.success).toBe(true);
    expect(presentation.clamped).toBe(-12.5);
    expect(isFailure(presentation)).toBe(false);
    for (const code of API_ERROR_CODES.filter((c) => c !== "value_out_of_range")) {
      expect(isFailure(presentationFor(make(code)))).toBe(true);
    }
  });

  it("carries retry_after into the countdown", () => {
    const presentation = presentationFor(make("rate_limited", { retry_after: 12.2 }));
    expect(presentation).toMatchObject({ kind: "countdown", retryAfter: 13 });
  });

  it("carries the request_id for internal_error so it can be copied", () => {
    const presentation = presentationFor(make("internal_error", {}, "01J8XQ4K2M"));
    expect(presentation).toMatchObject({ kind: "copyable", requestId: "01J8XQ4K2M" });
  });

  it("extracts field errors for validation_failed", () => {
    const presentation = presentationFor(make("validation_failed", { fields: { name: "Required" } }));
    expect(presentation).toMatchObject({ kind: "inline", fields: { name: "Required" } });
  });

  it("parses the envelope and falls back to internal_error for unknown codes", () => {
    const parsed = ApiError.fromEnvelope(403, {
      error: { code: "permission_denied", message: "No", detail: { reason: "first_run_incomplete" }, request_id: "abc" },
    });
    expect(parsed.code).toBe("permission_denied");
    expect(parsed.reason).toBe("first_run_incomplete");
    expect(parsed.requestId).toBe("abc");

    const unknown = ApiError.fromEnvelope(500, { error: { code: "something_new", message: "?" } });
    expect(unknown.code).toBe("internal_error");
    expect(isApiErrorCode("something_new")).toBe(false);

    const bare = ApiError.fromEnvelope(429, null);
    expect(bare.code).toBe("rate_limited");
  });
});
