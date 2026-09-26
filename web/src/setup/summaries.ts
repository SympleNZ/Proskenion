/*
 * A completed step, in one line (spec §10.4 *Resumability*: completed steps
 * show a summary with an edit option). The summary the API stores is safe to
 * display — never a password, never any secret — so this reads it directly.
 */
import type { SetupStep } from "./types";

function text(summary: Record<string, unknown>, key: string): string | null {
  const value = summary[key];
  return typeof value === "string" && value ? value : null;
}

function flag(summary: Record<string, unknown>, key: string): boolean {
  return summary[key] === true;
}

export function summaryText(step: SetupStep): string {
  const { summary } = step;
  switch (step.key) {
    case "welcome": {
      const parts = [text(summary, "locale"), text(summary, "timezone")].filter(Boolean);
      return parts.length ? parts.join(" · ") : "Confirmed";
    }
    case "admin_password":
      return flag(summary, "operator_seeded")
        ? "Admin password set; the operator account was seeded and is replaced at step 5"
        : "Admin password set";
    case "network": {
      if (flag(summary, "skipped")) return "Skipped — the network was already correct";
      const parts = [text(summary, "address"), text(summary, "hostname")].filter(Boolean);
      return parts.length ? parts.join(" · ") : "Reviewed";
    }
    case "devices": {
      if (flag(summary, "skipped")) return "Skipped — devices can be configured later";
      const count = summary["device_count"];
      if (typeof count === "number") return count === 1 ? "1 device configured" : `${count} devices configured`;
      return "Reviewed";
    }
    case "operator_password":
      return "Operator password set";
    case "certificate": {
      const parts: string[] = [];
      const option = text(summary, "option");
      const requestedLetsEncrypt = text(summary, "requested_option") === "lets_encrypt";
      if (requestedLetsEncrypt && option === "self_signed") {
        parts.push("Let's Encrypt failed — using a self-signed certificate instead");
      } else if (option === "self_signed") {
        parts.push("Self-signed certificate");
      } else if (option === "lets_encrypt") {
        parts.push("Let's Encrypt certificate");
      } else if (option) {
        parts.push(option);
      }
      const hostname = text(summary, "hostname");
      if (hostname) parts.push(hostname);
      const expires = text(summary, "expires");
      if (expires) parts.push(`expires ${expires}`);
      if (summary["nginx_reloaded"] === false) parts.push("nginx was not reloaded — it takes effect at the next reload");
      const fallbackReason = text(summary, "fallback_reason");
      if (fallbackReason) parts.push(fallbackReason);
      return parts.length ? parts.join(" · ") : "Issued";
    }
    case "summary":
      return flag(summary, "committed") ? "Committed" : "Reviewed";
  }
}
