/*
 * The build ID shown in the `?` sheet (spec §21.24 *Help*). `vite.config.ts`
 * computes it once, at config-evaluation time, from `git rev-parse` and the
 * current date — this only checks the shape actually reaching the bundle,
 * since the real hash varies commit to commit and the fallback needs no git
 * command to run.
 */
import { describe, expect, it } from "vitest";

import { BUILD_ID, BUILD_VERSION } from "./buildVersion";

describe("BUILD_ID", () => {
  it("is the git short hash (or 'unknown') and a YYYY-MM-DD date, separated by a middle dot", () => {
    expect(BUILD_ID).toMatch(/^([0-9a-f]{7,8}|unknown) · \d{4}-\d{2}-\d{2}$/);
  });

  it("is not the same string as the version — a build ID pins down a rebuild, a version does not", () => {
    expect(BUILD_ID).not.toBe(BUILD_VERSION);
  });
});
