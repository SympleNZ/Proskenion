/*
 * The streamed upload (spec §4.13, Q9, contracts §5 `POST /system/update`).
 * A package may be 2 GB, so this sends the file's raw bytes as the request
 * body — never `FormData`, which would hold the whole file in memory to
 * build a multipart envelope around it — and reports upload progress as it
 * goes. `fetch()` gives no upload progress event at all, so this is the one
 * place the interface reaches for `XMLHttpRequest` instead of `api()`.
 *
 * Verification happens entirely on the server once every byte has arrived
 * (`core/packages.py`); this module's job ends at hashing nothing and
 * inventing nothing — it hands back exactly what the server answered, or the
 * same `ApiError` the rest of the interface already knows how to read the
 * `rule` out of (contracts §3: "a rejection names the rule that refused it").
 */
import { API_BASE, ApiError, NetworkError } from "@/api/client";

import type { UploadResult } from "./types";

export const UPDATE_UPLOAD_PATH = `${API_BASE}/system/update`;

export interface UploadProgress {
  loaded: number;
  /** `null` when the browser could not compute a total (rare, but `lengthComputable` says so). */
  total: number | null;
}

export interface UploadPackageOptions {
  onProgress?: (progress: UploadProgress) => void;
  signal?: AbortSignal;
}

/**
 * `POST` a package's raw bytes and resolve with the verified manifest.
 * Rejects with `ApiError` for any non-2xx response (the same envelope
 * `api()` parses — §16.1), `NetworkError` if the request never completed,
 * and `DOMException("AbortError")` if `options.signal` fired.
 */
export function uploadPackage(file: File, options: UploadPackageOptions = {}): Promise<UploadResult> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", UPDATE_UPLOAD_PATH, true);
    xhr.withCredentials = true;
    xhr.responseType = "text";
    xhr.setRequestHeader("Accept", "application/json");

    xhr.upload.onprogress = (event) => {
      options.onProgress?.({ loaded: event.loaded, total: event.lengthComputable ? event.total : null });
    };

    xhr.onerror = () => reject(new NetworkError());

    xhr.onabort = () => reject(new DOMException("The upload was cancelled", "AbortError"));

    xhr.onload = () => {
      let body: unknown = null;
      if (xhr.responseText) {
        try {
          body = JSON.parse(xhr.responseText) as unknown;
        } catch {
          body = null;
        }
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(body as UploadResult);
      } else {
        reject(ApiError.fromEnvelope(xhr.status, body));
      }
    };

    const signal = options.signal;
    if (signal) {
      if (signal.aborted) {
        xhr.abort();
        return;
      }
      signal.addEventListener("abort", () => xhr.abort(), { once: true });
    }

    xhr.send(file);
  });
}
