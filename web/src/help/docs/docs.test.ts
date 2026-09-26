/*
 * The bundled documentation registry (spec §21.24 *Help*). Mostly a sanity
 * check that Vite's `?raw` import actually pulled in the real files — an
 * empty string or a stale copy would otherwise pass every other test here.
 */
import { describe, expect, it } from "vitest";

import { docsForTier, DOCS, getDoc } from "./docs";

describe("DOCS", () => {
  it("bundles all four real files with non-trivial content", () => {
    expect(DOCS).toHaveLength(4);
    for (const doc of DOCS) {
      expect(doc.raw.length).toBeGreaterThan(200);
    }
  });

  it("operator-quick-reference is the real file", () => {
    const doc = getDoc("operator-quick-reference");
    expect(doc.raw).toContain("Recalling a scene");
    expect(doc.raw).toContain("Who to call");
  });

  it("hire-handover is the real file", () => {
    expect(getDoc("hire-handover").raw).toContain("Before a hire");
  });

  it("recovery-card is the real file", () => {
    expect(getDoc("recovery-card").raw).toContain("Is it actually down?");
  });

  it("accessibility-check is the real file", () => {
    expect(getDoc("accessibility-check").raw).toContain("Screen reader — admin and operator");
  });
});

describe("docsForTier", () => {
  it("gives the hirer nothing", () => {
    expect(docsForTier("hirer")).toEqual([]);
  });

  it("gives the operator only the quick reference", () => {
    const ids = docsForTier("operator").map((d) => d.id);
    expect(ids).toEqual(["operator-quick-reference"]);
  });

  it("gives the admin all four", () => {
    const ids = docsForTier("admin").map((d) => d.id);
    expect(ids).toHaveLength(4);
    expect(ids).toContain("hire-handover");
    expect(ids).toContain("recovery-card");
    expect(ids).toContain("accessibility-check");
    expect(ids).toContain("operator-quick-reference");
  });
});
