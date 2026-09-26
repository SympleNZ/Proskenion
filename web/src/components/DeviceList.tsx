/* Device state as a plain list — the navigation rail's overflow on very short viewports (spec §21.7). */
import { StatusDot } from "@/components/ui/StatusDot";
import { DEVICE_LABELS, DEVICE_ORDER, STATUS_LABELS, useDeviceStatus, type DeviceName } from "@/live/deviceStatus";

function Row({ name }: { name: DeviceName }) {
  const { status } = useDeviceStatus(name);
  return (
    <li>
      <StatusDot status={status} subject={DEVICE_LABELS[name]} />
      <span>{DEVICE_LABELS[name]}</span>
      <span className="text-fg-muted ml-auto text-xs">{STATUS_LABELS[status]}</span>
    </li>
  );
}

export function DeviceList() {
  return (
    <ul aria-label="Devices">
      {DEVICE_ORDER.map((name) => (
        <Row key={name} name={name} />
      ))}
    </ul>
  );
}
