/* Formatting for the Updates screen: byte counts, and Pacific/Auckland dates and clocks (CONVENTIONS.md). */

const UNITS = ["B", "KB", "MB", "GB"] as const;

export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return "—";
  if (bytes < 1024) return `${bytes} B`;
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < UNITS.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const digits = value >= 100 || unit === 0 ? 0 : value >= 10 ? 1 : 2;
  return `${value.toFixed(digits)} ${UNITS[unit]}`;
}

export function formatDate(iso: string): string {
  const at = Date.parse(iso);
  if (Number.isNaN(at)) return iso;
  return new Date(at).toLocaleDateString("en-NZ", {
    day: "numeric",
    month: "long",
    year: "numeric",
    timeZone: "Pacific/Auckland",
  });
}

/** Date and 24-hour clock together, for a deadline that might not be today. */
export function formatDateTime(iso: string): string {
  const at = Date.parse(iso);
  if (Number.isNaN(at)) return iso;
  const date = new Date(at).toLocaleDateString("en-NZ", { day: "numeric", month: "short", timeZone: "Pacific/Auckland" });
  const clock = new Date(at).toLocaleTimeString("en-NZ", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: "Pacific/Auckland",
  });
  return `${date}, ${clock}`;
}
