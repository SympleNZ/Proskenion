/*
 * Banner (Appendix B37, §21.26): a toast reports an event, a banner reports a
 * state. Persistent conditions — reconnecting, offline, first-run — live here.
 */
import { CircleCheck, Info, OctagonX, TriangleAlert } from "lucide-react";
import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

export type BannerTone = "info" | "warning" | "danger" | "success";

export interface BannerProps {
  tone?: BannerTone;
  title?: ReactNode;
  children?: ReactNode;
  action?: ReactNode;
  className?: string;
  /**
   * Announce as a polite live region (the default). Off for a banner whose
   * condition `ConnectionAnnouncer` already speaks (device offline), so one
   * change is one announcement.
   */
  live?: boolean;
}

const ICONS: Record<BannerTone, typeof Info> = {
  info: Info,
  warning: TriangleAlert,
  danger: OctagonX,
  success: CircleCheck,
};

export function Banner({ tone = "info", title, children, action, className, live = true }: BannerProps) {
  const Icon = ICONS[tone];
  return (
    <div className={cn("banner", className)} data-tone={tone} role={live ? "status" : undefined} aria-live={live ? "polite" : undefined}>
      <Icon aria-hidden="true" className="banner-icon size-5" />
      <div className="banner-body">
        {title ? <div className="banner-title">{title}</div> : null}
        {children ? <div>{children}</div> : null}
      </div>
      {action}
    </div>
  );
}
