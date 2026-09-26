/*
 * `uploadPackage` (contracts §5 `POST /system/update`, Q9): raw-body upload
 * with progress, over a real `XMLHttpRequest` in the browser and a
 * controllable fake one here, since jsdom's own XHR would otherwise try to
 * make a real request.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, NetworkError } from "@/api/client";

import { uploadPackage } from "./upload";

class FakeUpload extends EventTarget {
  onprogress: ((event: ProgressEvent) => void) | null = null;
}

class FakeXHR {
  static instances: FakeXHR[] = [];

  method = "";
  url = "";
  status = 0;
  responseText = "";
  responseType = "";
  withCredentials = false;
  upload = new FakeUpload();
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onabort: (() => void) | null = null;
  sentBody: unknown;
  aborted = false;
  readonly headers: Record<string, string> = {};

  constructor() {
    FakeXHR.instances.push(this);
  }

  open(method: string, url: string): void {
    this.method = method;
    this.url = url;
  }

  setRequestHeader(name: string, value: string): void {
    this.headers[name] = value;
  }

  send(body: unknown): void {
    this.sentBody = body;
  }

  abort(): void {
    this.aborted = true;
    this.onabort?.();
  }

  progress(loaded: number, total: number, lengthComputable = true): void {
    this.upload.onprogress?.({ loaded, total, lengthComputable } as ProgressEvent);
  }

  respond(status: number, body: unknown): void {
    this.status = status;
    this.responseText = JSON.stringify(body);
    this.onload?.();
  }
}

beforeEach(() => {
  FakeXHR.instances = [];
  vi.stubGlobal("XMLHttpRequest", FakeXHR as unknown as typeof XMLHttpRequest);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function latest(): FakeXHR {
  const xhr = FakeXHR.instances.at(-1);
  if (!xhr) throw new Error("no XHR was opened");
  return xhr;
}

describe("uploadPackage", () => {
  it("sends the file's raw bytes, never FormData", async () => {
    const file = new File(["package bytes"], "auditorium_v1.3.0.aupkg");
    const promise = uploadPackage(file);
    const xhr = latest();
    expect(xhr.method).toBe("POST");
    expect(xhr.url).toContain("/system/update");
    expect(xhr.sentBody).toBe(file);
    xhr.respond(200, { manifest: { version: "v1.3.0" }, sha256: "abc", size: 13 });
    await expect(promise).resolves.toEqual({ manifest: { version: "v1.3.0" }, sha256: "abc", size: 13 });
  });

  it("reports upload progress as bytes stream", async () => {
    const file = new File(["x".repeat(100)], "big.aupkg");
    const seen: Array<{ loaded: number; total: number | null }> = [];
    const promise = uploadPackage(file, { onProgress: (p) => seen.push(p) });
    const xhr = latest();
    xhr.progress(10, 100);
    xhr.progress(100, 100);
    xhr.respond(200, { manifest: {}, sha256: "x", size: 100 });
    await promise;
    expect(seen).toEqual([
      { loaded: 10, total: 100 },
      { loaded: 100, total: 100 },
    ]);
  });

  it("reports no total when the browser could not compute one", async () => {
    const file = new File(["x"], "a.aupkg");
    const seen: Array<{ loaded: number; total: number | null }> = [];
    const promise = uploadPackage(file, { onProgress: (p) => seen.push(p) });
    const xhr = latest();
    xhr.progress(10, 0, false);
    xhr.respond(200, { manifest: {}, sha256: "x", size: 1 });
    await promise;
    expect(seen).toEqual([{ loaded: 10, total: null }]);
  });

  it("rejects with the same ApiError shape a rejected package carries — a rule and a plain sentence", async () => {
    const promise = uploadPackage(new File(["bad"], "bad.aupkg"));
    const xhr = latest();
    xhr.respond(422, {
      error: {
        code: "validation_failed",
        message: "This package could not be verified. It may be corrupted, or it was not built with a trusted signing key.",
        detail: { rule: "signature", reason: "signature invalid" },
      },
    });
    await expect(promise).rejects.toBeInstanceOf(ApiError);
    try {
      await promise;
    } catch (error) {
      expect(error).toBeInstanceOf(ApiError);
      const apiError = error as ApiError;
      expect(apiError.code).toBe("validation_failed");
      expect(apiError.detail["rule"]).toBe("signature");
      expect(apiError.message).toBe(
        "This package could not be verified. It may be corrupted, or it was not built with a trusted signing key.",
      );
    }
  });

  it("rejects with NetworkError when the request never completes", async () => {
    const promise = uploadPackage(new File(["x"], "a.aupkg"));
    latest().onerror?.();
    await expect(promise).rejects.toBeInstanceOf(NetworkError);
  });

  it("aborts the underlying request and rejects with AbortError when the signal fires", async () => {
    const controller = new AbortController();
    const promise = uploadPackage(new File(["x"], "a.aupkg"), { signal: controller.signal });
    const xhr = latest();
    controller.abort();
    expect(xhr.aborted).toBe(true);
    await expect(promise).rejects.toMatchObject({ name: "AbortError" });
  });

  it("aborts immediately for a signal that is already aborted", async () => {
    const controller = new AbortController();
    controller.abort();
    const promise = uploadPackage(new File(["x"], "a.aupkg"), { signal: controller.signal });
    expect(latest().aborted).toBe(true);
    await expect(promise).rejects.toMatchObject({ name: "AbortError" });
  });
});
