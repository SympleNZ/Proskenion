/*
 * Formatting for the Health screen (spec §21.24 *Health*, §5.4).
 *
 * One rule runs through all of it: an unsupported metric reads "not
 * available". A missing number is never rendered as zero, a dash or a blank —
 * zero degrees and zero media errors are real readings and must stay
 * distinguishable from a reading that could not be taken.
 */

export const NOT_AVAILABLE = "not available";

const UNITS = ["bytes", "kB", "MB", "GB", "TB"] as const;

export function formatBytes(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return NOT_AVAILABLE;
  if (value < 1000) return `${Math.round(value)} bytes`;
  let scaled = value;
  let unit = 0;
  while (scaled >= 1000 && unit < UNITS.length - 1) {
    scaled /= 1000;
    unit += 1;
  }
  // 16.0 GB reads as false precision; 4.2 GB does not.
  return `${scaled >= 100 ? Math.round(scaled) : Number(scaled.toFixed(1))} ${UNITS[unit]}`;
}

export function formatPercent(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return NOT_AVAILABLE;
  return `${value < 10 ? Number(value.toFixed(1)) : Math.round(value)} %`;
}

export function formatTemperature(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return NOT_AVAILABLE;
  return `${Math.round(value)} °C`;
}

export function formatCount(value: number | null | undefined, suffix = ""): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return NOT_AVAILABLE;
  return suffix ? `${value} ${suffix}` : String(value);
}

export function formatMilliseconds(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return NOT_AVAILABLE;
  return `${value < 10 ? Number(value.toFixed(1)) : Math.round(value)} ms`;
}

/** "4 h 12 m", "18 d 3 h", "42 s" — enough to see whether it has just restarted. */
export function formatUptime(seconds: number | null | undefined): string {
  if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds < 0) return NOT_AVAILABLE;
  const whole = Math.floor(seconds);
  const days = Math.floor(whole / 86400);
  const hours = Math.floor((whole % 86400) / 3600);
  const minutes = Math.floor((whole % 3600) / 60);
  if (days > 0) return `${days} d ${hours} h`;
  if (hours > 0) return `${hours} h ${minutes} m`;
  if (minutes > 0) return `${minutes} m`;
  return `${whole} s`;
}

/** "4.2 GB / 16 GB", or "not available" when either half is missing. */
export function formatUsage(used: number | null | undefined, total: number | null | undefined): string {
  if (typeof used !== "number" || typeof total !== "number") return NOT_AVAILABLE;
  return `${formatBytes(used)} / ${formatBytes(total)}`;
}

/** A backup medium that has been absent since a timestamp says how long, in words. */
export function formatAbsence(absentSince: string | null): string | null {
  if (!absentSince) return null;
  const since = Date.parse(absentSince);
  if (Number.isNaN(since)) return `absent since ${absentSince}`;
  const minutes = Math.max(0, Math.round((Date.now() - since) / 60000));
  if (minutes < 60) return `absent for ${minutes} min`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `absent for ${hours} h`;
  return `absent for ${Math.round(hours / 24)} days`;
}
