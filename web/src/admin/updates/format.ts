/* Formatting for the Updates screen: byte counts, and Pacific/Auckland dates and clocks (CONVENTIONS.md). */
import { formatDate as formatDateShared, formatDateTime as formatDateTimeShared } from "@/lib/time";

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
  return formatDateShared(iso);
}

/** Date and 24-hour clock together, for a deadline that might not be today. */
export function formatDateTime(iso: string): string {
  return formatDateTimeShared(iso);
}
