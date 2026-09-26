/*
 * The axe-core wrapper every screen's accessibility sweep runs through
 * (spec §24, D4). `axe-core`/`vitest-axe` are dev dependencies only —
 * nothing here is imported by application code, so none of it reaches the
 * appliance bundle (web/package.json's devDependencies; verified by
 * `git diff main -- web/package.json` showing no `dependencies` change).
 *
 * "serious" and "critical" findings gate the suite (spec §24.7's checklist
 * is the pass bar this maps to: focus, traps, labels, contrast, targets —
 * things a screen-reader or keyboard user cannot work around). "moderate"
 * and "minor" findings are collected rather than failing the build: axe's
 * own heuristics flag some things that need a human to judge in context
 * (for example a landmark uniqueness rule tripping on a test fixture that
 * mounts two screens' worth of markup at once), and a report a screen
 * author cannot act on is not doing its job, so every moderate/minor
 * finding is listed rather than silently dropped.
 */
import { axe } from "vitest-axe";
import type { Result } from "axe-core";

const BLOCKING_IMPACTS = new Set(["serious", "critical"]);

export interface AxeSweepResult {
  /** "serious" and "critical" — the suite fails if this is non-empty. */
  blocking: Result[];
  /** "moderate" and "minor" — reported, not gated. */
  reported: Result[];
}

/** Runs axe-core against a rendered container and splits its violations by impact. */
export async function sweepA11y(container: Element): Promise<AxeSweepResult> {
  const results = await axe(container, {
    // jsdom has no layout engine, so axe's own colour-contrast check (which
    // needs computed, rendered styles) is unreliable here and is covered
    // properly instead by contrast.test.ts, computed straight from the
    // tokens. Leaving it enabled produces false passes and false failures
    // depending on jsdom's stub CSS support, neither of which is signal.
    rules: { "color-contrast": { enabled: false } },
  });
  const blocking = results.violations.filter((violation) => BLOCKING_IMPACTS.has(violation.impact ?? ""));
  const reported = results.violations.filter((violation) => !BLOCKING_IMPACTS.has(violation.impact ?? ""));
  return { blocking, reported };
}

/**
 * `heading-order` alone, at "moderate" impact by default (axe-core's own
 * rule table) — `sweepA11y` above only gates on "serious"/"critical", so a
 * skipped heading level never fails the general sweep. A later fix
 * renumbered Backup, Certificates, Email, Health, Help, Hirer Access,
 * Logs, Network, Rules and Updates to a clean h1→h2→h3 outline; asserting
 * this rule directly, on those ten screens, means the skip that fix
 * corrected cannot recur silently under the "moderate" ceiling the rest of
 * the sweep still uses.
 */
export async function headingOrderViolations(container: Element): Promise<Result[]> {
  const results = await axe(container, { runOnly: { type: "rule", values: ["heading-order"] } });
  return results.violations;
}

/** One violation, one line: impact, rule id, what to fix, how many elements, and the axe-core explainer link. */
function describeViolation(violation: Result): string {
  const nodeCount = violation.nodes.length;
  return `${violation.impact}: ${violation.id} — ${violation.help} (${nodeCount} element${nodeCount === 1 ? "" : "s"})\n    ${violation.helpUrl}`;
}

export function describeViolations(violations: readonly Result[]): string {
  return violations.map(describeViolation).join("\n");
}
