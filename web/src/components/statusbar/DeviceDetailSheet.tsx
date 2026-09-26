/* Device detail sheet (spec §21.7 wireframe). Staff only; "—" for anything absent. */
import { Link } from "react-router-dom";

import type { Tier } from "@/api/auth";
import { Sheet, SheetContent } from "@/components/ui/Sheet";
import { StatusDot } from "@/components/ui/StatusDot";
import { DEVICE_LABELS, STATUS_LABELS, useDeviceStatus, type DeviceName } from "@/live/deviceStatus";
import { formatRelative } from "@/lib/time";

/** Said verbatim when the mixer is refused rather than timed out (§21.7). */
export const MIXER_REFUSED_MESSAGE =
  "Another MIDI client holds the connection. Check that MixPad is using the CQ's WiFi rather than Ethernet.";

const dash = "—";

function DeviceDetailBody({ name, tier }: { name: DeviceName; tier: Tier }) {
  const { status, detail } = useDeviceStatus(name);
  const label = DEVICE_LABELS[name];
  return (
    <>
      <dl className="kv">
        <dt>Status</dt>
        <dd className="flex items-center gap-2">
          <StatusDot status={status} subject={label} />
          <span>{STATUS_LABELS[status]}</span>
        </dd>
        <dt>Last seen</dt>
        <dd>{formatRelative(detail?.last_seen)}</dd>
        <dt>Host</dt>
        <dd className="technical">{detail?.host ?? dash}</dd>
        <dt>Port</dt>
        <dd className="technical">{detail?.port ?? dash}</dd>
        <dt>Protocol</dt>
        <dd>{detail?.protocol ?? dash}</dd>
        <dt>Latency</dt>
        <dd className="technical">{detail?.latency_ms != null ? `${detail.latency_ms} ms average` : dash}</dd>
        <dt>Reconnects</dt>
        <dd className="technical">{detail?.reconnects != null ? `${detail.reconnects} since last restart` : dash}</dd>
        <dt>Last error</dt>
        <dd className="technical">{detail?.last_error ?? dash}</dd>
      </dl>
      {detail?.kind === "refused" && (
        <p className="note text-warning-text" role="status">
          {MIXER_REFUSED_MESSAGE}
        </p>
      )}
      {tier === "admin" && (
        <Link to="/admin/devices" className="btn btn-secondary self-start">
          Go to device settings
        </Link>
      )}
    </>
  );
}

export interface DeviceDetailSheetProps {
  name: DeviceName | null;
  tier: Tier;
  onClose(): void;
}

export function DeviceDetailSheet({ name, tier, onClose }: DeviceDetailSheetProps) {
  return (
    <Sheet open={name !== null} onOpenChange={(open) => !open && onClose()}>
      {name && <DeviceDetailSheetContent name={name} tier={tier} />}
    </Sheet>
  );
}

function DeviceDetailSheetContent({ name, tier }: { name: DeviceName; tier: Tier }) {
  const { detail } = useDeviceStatus(name);
  return (
    <SheetContent title={detail?.name ?? DEVICE_LABELS[name]} description={detail?.name ? DEVICE_LABELS[name] : undefined}>
      <DeviceDetailBody name={name} tier={tier} />
    </SheetContent>
  );
}
