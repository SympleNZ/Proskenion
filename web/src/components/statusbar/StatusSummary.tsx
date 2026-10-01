/*
 * Phone-width summary LED (owner's decision, 2026-09; not yet in the spec's
 * own §21.7 — §21.7 shows five indicators, each tapped on its own, with no
 * width exception; see WORKLOG for the coordinator's note on this
 * deviation). At a phone's width there is no room to read five dots, so
 * this collapses them to the worst state (`summariseDeviceStatus`,
 * `@/live/deviceStatus`) behind one LED, and moves the detail behind a tap
 * rather than losing it: the popover lists every indicator by name and
 * state, and — for staff, where the full-size bar already opens the detail
 * sheet on tap — selecting one opens that same sheet, so nothing the
 * desktop bar offers is unreachable here. Built on `Menu` (Radix
 * `DropdownMenu`), the same primitive `AccountChip` uses, so it positions
 * and dismisses (outside tap, Escape) the same way.
 *
 * Hidden above the phone breakpoint by `components.css`
 * (`@media (max-width: theme(--breakpoint-tablet))`), which also hides the
 * five-dot row and the timer it replaces at that width.
 */
import { StatusDot } from "@/components/ui/StatusDot";
import { Menu, MenuContent, MenuItem, MenuLabel, MenuSeparator, MenuTrigger } from "@/components/ui/Menu";
import {
  DEVICE_LABELS,
  DEVICE_ORDER,
  STATUS_LABELS,
  summariseDeviceStatus,
  useDeviceStatuses,
  type DeviceName,
  type DeviceStatus,
} from "@/live/deviceStatus";

/** Everything short of fully connected and not merely unset — what the accessible name calls out by name. */
function isUnhealthy(status: DeviceStatus): boolean {
  return status !== "connected" && status !== "unconfigured";
}

/**
 * "Connections: Mixer degraded, HDMI offline" — or "Connections: all
 * connected" when nothing needs calling out. Unconfigured devices are
 * never named individually, same as they never move the summary LED
 * itself, but a rig where nothing at all is configured reads as "not
 * configured" rather than the misleading "all connected".
 */
function summaryAccessibleName(
  entries: readonly { name: DeviceName; status: DeviceStatus }[],
  summary: DeviceStatus,
): string {
  const unhealthy = entries.filter((entry) => isUnhealthy(entry.status));
  if (unhealthy.length > 0) {
    const words = unhealthy.map((entry) => `${DEVICE_LABELS[entry.name]} ${STATUS_LABELS[entry.status].toLowerCase()}`);
    return `Connections: ${words.join(", ")}`;
  }
  if (summary === "unconfigured") return "Connections: not configured";
  return "Connections: all connected";
}

function ConnectionRow({
  name,
  status,
  onOpen,
}: {
  name: DeviceName;
  status: DeviceStatus;
  onOpen: ((name: DeviceName) => void) | undefined;
}) {
  const label = DEVICE_LABELS[name];
  const body = (
    <>
      <StatusDot status={status} subject={label} />
      <span className="connection-row-label">{label}</span>
      <span className="connection-row-state">{STATUS_LABELS[status]}</span>
    </>
  );
  if (!onOpen) {
    // Hirers see the list with no interaction (§21.7: "Hirers see indicators
    // only, with no interaction") — same rule the full-size bar's dots
    // follow. No aria-label here: the nested StatusDot already carries
    // "Mixer: Offline" as an accessible image, and a second, identical
    // label on this wrapper would just be the same words read twice.
    return <div className="menu-item connection-row">{body}</div>;
  }
  return (
    <MenuItem className="connection-row" onSelect={() => onOpen(name)}>
      {body}
    </MenuItem>
  );
}

export function StatusSummary({ onOpen }: { onOpen: ((name: DeviceName) => void) | undefined }) {
  const statuses = useDeviceStatuses();
  const entries = DEVICE_ORDER.map((name) => ({ name, status: statuses[name] }));
  const summary = summariseDeviceStatus(entries.map((entry) => entry.status));
  const accessibleName = summaryAccessibleName(entries, summary);

  return (
    <Menu modal={false}>
      <MenuTrigger asChild>
        {/* Not a live region (`ConnectionAnnouncer` speaks changes). Only one of
            this and the five-dot row is ever in the accessibility tree:
            `components.css`'s media query removes the other with `display: none`. */}
        <button type="button" className="status-summary" aria-label={accessibleName}>
          <StatusDot status={summary} subject="Connections" size="lg" />
        </button>
      </MenuTrigger>
      <MenuContent align="start" side="top">
        <MenuLabel>Connections</MenuLabel>
        <MenuSeparator />
        {entries.map((entry) => (
          <ConnectionRow key={entry.name} name={entry.name} status={entry.status} onOpen={onOpen} />
        ))}
      </MenuContent>
    </Menu>
  );
}
