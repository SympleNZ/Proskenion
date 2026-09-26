/*
 * Status dot (spec §21.3 quick reference, §21.7, §24.1). Colour and icon,
 * never colour alone: healthy is the filled dot, degraded ⚠, offline ✕, not
 * configured ○, connecting ⟳ — the transport is being established (§5.3),
 * before a probe has said whether the device is actually there. The
 * accessible name carries the words.
 */
import { Circle, Loader2, TriangleAlert, X } from "lucide-react";

import { STATUS_LABELS, type DeviceStatus } from "@/live/deviceStatus";
import { cn } from "@/lib/utils";

export type StatusIcon = "filled" | "warning" | "cross" | "hollow" | "spinner";

const STATUS_ICONS: Readonly<Record<DeviceStatus, StatusIcon>> = {
  connecting: "spinner",
  connected: "filled",
  degraded: "warning",
  error: "cross",
  unconfigured: "hollow",
};

export interface StatusDotProps {
  status: DeviceStatus;
  /** What the dot describes, e.g. "Mixer". Becomes "Mixer: Degraded". */
  subject: string;
  size?: "sm" | "lg";
  className?: string;
}

export function StatusDot({ status, subject, size = "sm", className }: StatusDotProps) {
  const icon = STATUS_ICONS[status];
  const label = STATUS_LABELS[status];
  return (
    <span
      className={cn("status-dot", size === "lg" && "status-dot-lg", className)}
      data-status={status}
      data-icon={icon}
      data-label={label}
      role="img"
      aria-label={`${subject}: ${label}`}
    >
      {icon === "warning" && <TriangleAlert aria-hidden="true" strokeWidth={3} />}
      {icon === "cross" && <X aria-hidden="true" strokeWidth={3} />}
      {icon === "hollow" && <Circle aria-hidden="true" strokeWidth={2.5} />}
      {icon === "spinner" && <Loader2 aria-hidden="true" strokeWidth={3} />}
    </span>
  );
}
