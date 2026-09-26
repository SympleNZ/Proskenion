/*
 * Serial device picker (spec §21.24 *Serial device picker*, §5.5 *Serial port
 * enumeration*).
 *
 * Ports are enumerated from /dev/serial/by-id/ and the by-id path is what
 * gets stored — never ttyUSB0, whose numbering follows enumeration order and
 * moves on replug (§4.12, B46). Refresh re-enumerates because hot-plugging
 * during commissioning is normal and a list captured at page load is wrong
 * within a minute.
 *
 * The two flags earn their place, and both are words as well as colour
 * (§24.1): *in use* names the holder, surfacing §7.5's device-busy failure at
 * configuration time rather than at first connect; *no stable path* says
 * plainly that the cable will move, while that can still be solved by buying
 * a different one.
 */
import { RefreshCw, Search } from "lucide-react";
import { useId } from "react";

import { Banner } from "@/components/ui/Banner";
import { Button } from "@/components/ui/Button";
import { Skeleton } from "@/components/ui/EmptyState";
import { cn } from "@/lib/utils";

import { useSerialPorts } from "./api";
import type { SerialPort } from "./types";

/**
 * Identify opens the port and runs the driver's probe before the device is
 * saved (§21.24). The route does not exist yet, so the button is disabled and
 * says why rather than being wired to nothing.
 */
export const IDENTIFY_UNAVAILABLE =
  "Identify needs a backend route that does not exist yet. Save the device and use Test connection instead.";

export interface SerialPickerProps {
  /** The stored by-id path, or "" when nothing is chosen yet. */
  value: string;
  onChange: (path: string) => void;
  id: string;
  describedBy?: string | undefined;
  invalid?: boolean;
  disabled?: boolean;
}

function vendorProduct(port: SerialPort): string | null {
  if (!port.vendor_id || !port.product_id) return null;
  return `${port.vendor_id}:${port.product_id}`;
}

export function SerialPicker({ value, onChange, id, describedBy, invalid, disabled = false }: SerialPickerProps) {
  const query = useSerialPorts(true);
  const groupName = useId();
  const ports = query.data?.ports ?? [];
  const known = ports.some((port) => port.path === value);
  const selected = ports.find((port) => port.path === value);

  return (
    <div className="serial-picker" id={id} aria-describedby={describedBy} data-invalid={invalid ? "true" : undefined}>
      <div className="serial-actions">
        <Button
          variant="secondary"
          onClick={() => void query.refetch()}
          loading={query.isFetching}
          disabled={disabled}
          aria-label="Refresh the list of serial devices"
        >
          <RefreshCw aria-hidden="true" className="size-4" />
          <span>Refresh</span>
        </Button>
        <Button variant="ghost" disabled aria-disabled="true" title={IDENTIFY_UNAVAILABLE} aria-describedby={`${id}-identify`}>
          <Search aria-hidden="true" className="size-4" />
          <span>Identify</span>
        </Button>
      </div>
      <p className="field-help" id={`${id}-identify`}>
        {IDENTIFY_UNAVAILABLE}
      </p>

      {query.isPending ? (
        <div className="serial-list" aria-busy="true" aria-label="Enumerating serial devices">
          <Skeleton className="h-touch w-full" />
          <Skeleton className="h-touch w-full" />
        </div>
      ) : query.isError ? (
        <Banner tone="danger" title="Could not list the serial devices">
          The controller did not answer the enumeration request.{" "}
          <Button variant="ghost" onClick={() => void query.refetch()}>
            Try again
          </Button>
        </Banner>
      ) : ports.length === 0 ? (
        <Banner tone="warning" title="No serial devices found">
          Nothing is plugged in, or the cable presents no by-id entry. Plug the cable in and choose Refresh.
        </Banner>
      ) : (
        <div className="serial-list" role="radiogroup" aria-label="Available serial devices">
          {ports.map((port) => {
            const optionId = `${id}-${port.path.replace(/[^\w-]/g, "-")}`;
            return (
              <label className={cn("serial-option", port.path === value && "is-selected")} key={port.path} htmlFor={optionId}>
                <input
                  type="radio"
                  id={optionId}
                  name={groupName}
                  value={port.path}
                  checked={port.path === value}
                  disabled={disabled}
                  onChange={() => onChange(port.path)}
                />
                <span className="serial-option-body">
                  <span className="serial-option-title">
                    <span>{port.label}</span>
                    {vendorProduct(port) ? <span className="technical">{vendorProduct(port)}</span> : null}
                  </span>
                  <span className="serial-flags">
                    {port.in_use ? (
                      <span className="flag" data-tone="warning">
                        in use{port.in_use_by ? ` — ${port.in_use_by}` : ""}
                      </span>
                    ) : null}
                    {!port.stable ? (
                      <span className="flag" data-tone="warning">
                        no stable path — this cable has no serial number, so its path will move when it is replugged
                      </span>
                    ) : null}
                  </span>
                  <span className="serial-path technical">{port.path}</span>
                </span>
              </label>
            );
          })}
        </div>
      )}

      {value ? (
        <p className="serial-selected">
          <span className="field-label">Stored path</span>
          <span className="serial-path technical">{value}</span>
          {!known && !query.isPending ? (
            <span className="flag" data-tone="warning">
              not present now — the cable is unplugged, or it has been replaced
            </span>
          ) : null}
          {selected?.in_use ? (
            <span className="flag" data-tone="warning">
              in use{selected.in_use_by ? ` — ${selected.in_use_by}` : ""}
            </span>
          ) : null}
        </p>
      ) : null}
    </div>
  );
}
