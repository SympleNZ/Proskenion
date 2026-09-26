/*
 * A plain-language preview for a five-field cron expression (spec §21.17:
 * "a cron field with a plain-language preview"). This covers the shapes
 * §8.8's own examples use — a fixed daily time, a fixed time on named days,
 * a simple minute or hour interval — and falls back to naming the five
 * fields for anything else rather than guessing. It never validates: that is
 * `proskenion/rules/model.py`'s `validate_cron`, enforced server-side and
 * surfaced as a `422` on the `cron` field (§21.17).
 */
const DAY_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];

function pad(n: number): string {
  return n.toString().padStart(2, "0");
}

/** A human description of `expression`, or `null` if it does not parse as five fields. */
export function cronPreview(expression: string): string | null {
  const parts = expression.trim().split(/\s+/);
  if (parts.length !== 5) return null;
  const [minute, hour, dom, month, dow] = parts as [string, string, string, string, string];

  if (dom === "*" && month === "*") {
    const time = fixedTime(minute, hour);
    if (time) {
      if (dow === "*") return `${time} daily`;
      const day = namedDay(dow);
      if (day) return `${time} on ${day}`;
    }
  }

  if (minute.startsWith("*/") && hour === "*" && dom === "*" && month === "*" && dow === "*") {
    const step = Number(minute.slice(2));
    if (Number.isFinite(step) && step > 0) return `every ${step} minute${step === 1 ? "" : "s"}`;
  }
  if (minute === "0" && hour.startsWith("*/") && dom === "*" && month === "*" && dow === "*") {
    const step = Number(hour.slice(2));
    if (Number.isFinite(step) && step > 0) return `every ${step} hour${step === 1 ? "" : "s"}, on the hour`;
  }

  return `minute ${minute} · hour ${hour} · day of month ${dom} · month ${month} · day of week ${dow}`;
}

function fixedTime(minute: string, hour: string): string | null {
  if (!/^\d{1,2}$/.test(minute) || !/^\d{1,2}$/.test(hour)) return null;
  const m = Number(minute);
  const h = Number(hour);
  if (m > 59 || h > 23) return null;
  return `${pad(h)}:${pad(m)}`;
}

function namedDay(dow: string): string | null {
  if (!/^\d$/.test(dow)) return null;
  const n = Number(dow) % 7; // 7 is also Sunday
  return DAY_NAMES[n] ?? null;
}
