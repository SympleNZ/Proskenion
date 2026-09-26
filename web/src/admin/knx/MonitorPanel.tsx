/*
 * The live telegram monitor (§21.19 *Live monitor*): "A collapsible panel
 * showing incoming telegrams in real time, over server-sent events...
 * Registered addresses show their name; unknown ones offer to be added...
 * Filterable, and pausable." Connected only while this panel is expanded —
 * collapsing it, switching tabs away from it, or leaving the screen all
 * unmount it the same way, which is what closes the SSE connection (see
 * `monitor.ts`).
 *
 * Also shows `GET /knx/unsupported` (§7.1 *Unsupported types*): telegrams on
 * a *registered* address whose DPT has no codec, so the integrator can see
 * what needs a codec without hunting through logs.
 */
import { ChevronDown, Circle, Loader2, Pause } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { Input } from "@/components/ui/Input";
import { FieldLabel } from "@/help/HelpButton";

import { useUnsupported } from "./api";
import { useKnxMonitor, type CreateEventSource } from "./monitor";

/** Colour and icon together, never colour alone (§24.1) — the monitor's own
 * three states are not the device-status vocabulary `StatusDot` draws from
 * (connected/degraded/error/…), so this pairs its own icon with its own text
 * rather than stretching that unrelated set to fit. */
function MonitorStatus({ connected, paused }: { connected: boolean; paused: boolean }) {
  if (paused) {
    return (
      <span className="pill" data-tone="warning" role="status">
        <Pause aria-hidden="true" className="size-3" /> Paused
      </span>
    );
  }
  if (!connected) {
    return (
      <span className="pill" role="status">
        <Loader2 aria-hidden="true" className="size-3" /> Connecting…
      </span>
    );
  }
  return (
    <span className="pill" data-tone="success" role="status">
      <Circle aria-hidden="true" className="size-3" fill="currentColor" /> Recording
    </span>
  );
}

export interface MonitorPanelProps {
  onAddToLibrary: (groupAddress: string) => void;
  /** Test-only: an injectable `EventSource` factory, the same seam
   * `live/socket.ts`'s `LiveSocket` uses for its WebSocket. */
  createSource?: CreateEventSource | undefined;
  /** Test-only: overrides the default 200-entry bound so a test does not
   * have to push hundreds of telegrams to prove the list is bounded. */
  maxEntries?: number | undefined;
}

export function MonitorPanel({ onAddToLibrary, createSource, maxEntries }: MonitorPanelProps) {
  const [expanded, setExpanded] = useState(false);
  const [filter, setFilter] = useState("");
  const monitor = useKnxMonitor(expanded, { createSource, maxEntries });
  const unsupported = useUnsupported(expanded);

  const entries = filter.trim() ? monitor.entries.filter((entry) => entry.group_address.includes(filter.trim())) : monitor.entries;

  return (
    <Card className="device-card" data-testid="knx-monitor-panel">
      <header className="device-head">
        <button
          type="button"
          className="btn btn-ghost device-title"
          aria-expanded={expanded}
          onClick={() => setExpanded((value) => !value)}
        >
          <ChevronDown aria-hidden="true" className="size-5 chevron" data-expanded={expanded} />
          <span className="card-title">Live monitor</span>
        </button>
        {expanded ? (
          <div className="device-actions">
            <MonitorStatus connected={monitor.connected} paused={monitor.paused} />
            <Button variant="secondary" onClick={monitor.paused ? monitor.resume : monitor.pause}>
              {monitor.paused ? "Resume" : "Pause"}
            </Button>
            <Button variant="secondary" onClick={monitor.clear}>
              Clear
            </Button>
          </div>
        ) : null}
      </header>

      {expanded ? (
        <>
          <div className="field schema-field">
            <FieldLabel htmlFor="knx-monitor-filter" help="knx.monitor.filter">
              Filter by address
            </FieldLabel>
            <Input
              id="knx-monitor-filter"
              placeholder="e.g. 1/0"
              value={filter}
              onChange={(event) => setFilter(event.currentTarget.value)}
            />
          </div>

          {entries.length === 0 ? (
            <p className="metric-note">No telegrams observed yet — press a wall panel to see what it sends.</p>
          ) : (
            <div className="table-scroll">
              <table className="data-table">
                <caption className="sr-only">Live KNX telegrams</caption>
                <thead>
                  <tr>
                    <th scope="col">Time</th>
                    <th scope="col">Address</th>
                    <th scope="col">Value</th>
                    <th scope="col">DPT</th>
                    <th scope="col">
                      <span className="sr-only">Add</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {entries.map((entry, index) => (
                    <tr key={`${entry.timestamp}-${entry.group_address}-${index}`}>
                      <td className="technical">{entry.timestamp}</td>
                      <td className="technical">{entry.group_address}</td>
                      <td className="technical">{entry.dpt === null ? String(entry.value ?? "—") : JSON.stringify(entry.value)}</td>
                      <td className="technical">{entry.dpt ?? "unknown"}</td>
                      <td>
                        {entry.dpt === null ? (
                          <Button variant="secondary" onClick={() => onAddToLibrary(entry.group_address)}>
                            Add to library
                          </Button>
                        ) : null}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {unsupported.data && unsupported.data.length > 0 ? (
            <section className="device-section" aria-labelledby="knx-unsupported-title">
              <h4 className="sect-label" id="knx-unsupported-title">
                Unsupported types
              </h4>
              <ul className="connection-list">
                {unsupported.data.map((entry, index) => (
                  <li className="connection-row" key={`${entry.group_address}-${index}`}>
                    <span className="connection-name technical">{entry.group_address}</span>
                    <span>{entry.name ?? "Unregistered"}</span>
                    <span className="technical">DPT {entry.dpt}</span>
                    <span className="connection-purpose technical">{entry.timestamp}</span>
                  </li>
                ))}
              </ul>
            </section>
          ) : null}
        </>
      ) : null}
    </Card>
  );
}
