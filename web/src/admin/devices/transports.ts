/*
 * Transport names (spec §5.5 *Transport is separate from protocol*, B45).
 * A transport picker appears only where a driver supports more than one; the
 * driver itself never mentions a host or a device path.
 */
const TRANSPORT_LABELS: Readonly<Record<string, string>> = {
  tcp: "Ethernet (TCP)",
  udp: "Ethernet (UDP)",
  serial: "Serial",
  unix: "Local socket",
  loopback: "Loopback (no hardware)",
};

export function transportLabel(type: string): string {
  return TRANSPORT_LABELS[type] ?? type;
}
