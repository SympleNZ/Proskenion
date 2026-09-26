/* §21.24's expiry indicator: green over 30 days, amber 7 to 30, red under 7 (or expired). */
import type { Level } from "@/components/ui/levels";

import type { CertificateCard } from "./types";

export function certificateLevel(card: CertificateCard | null): Level {
  if (!card) return "unknown";
  if (card.expired) return "red";
  if (card.days_remaining > 30) return "green";
  if (card.days_remaining >= 7) return "amber";
  return "red";
}
