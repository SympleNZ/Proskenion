/* Formatting for the Backup screen's cards (spec §21.24). */
import { formatBytes } from "@/admin/health/format";
import { daysSince, formatDate as formatDateShared, formatTime } from "@/lib/time";

export { formatBytes };

/** "12 March 2026" — the same long form the Certificates and Network screens use. */
export function formatDate(iso: string): string {
  return formatDateShared(iso);
}

/** "Today 03:00", "Yesterday 03:00", "12 March 2026" — §21.24's history rows. Day boundaries are
 * the appliance's own, Pacific/Auckland (`daysSince`), not the viewing device's. */
export function formatWhen(iso: string): string {
  const at = Date.parse(iso);
  if (Number.isNaN(at)) return iso;
  const time = formatTime(iso);
  const days = daysSince(at);
  if (days === 0) return `Today ${time}`;
  if (days === 1) return `Yesterday ${time}`;
  if (days < 7) return `${days} days ago`;
  return formatDate(iso);
}

/** "Today", "Yesterday", "11 days" — §21.24's system images list. */
export function formatAge(iso: string): string {
  const at = Date.parse(iso);
  if (Number.isNaN(at)) return iso;
  const days = daysSince(at);
  if (days === 0) return "Today";
  if (days === 1) return "Yesterday";
  return `${days} days`;
}

/** "hirer_max_db" → "hirer max db" (`admin/devices/types.ts`'s `humanise`, without the capital — a
 * field name reads better lower-case inline in a sentence: "hirer max db changed"). */
export function fieldLabel(field: string): string {
  return field.replace(/_/g, " ").trim();
}
