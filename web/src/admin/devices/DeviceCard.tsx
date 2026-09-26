/*
 * One configured driver instance (spec §21.24 *Devices*).
 *
 * The card carries the driver's name and key, its live status, the two
 * configuration sections, the per-connection list where a driver holds more
 * than one, capabilities as connected, and a test that reports both stages.
 *
 * Saving attempts an immediate reconnect. On failure the API restores the
 * previous settings and says so, and this card shows that plainly: a wrong
 * address should not leave the system unable to reach a working device.
 */
import { useRef, useState } from "react";

import { ApiError } from "@/api/client";
import { presentError } from "@/api/errors";
import { Banner } from "@/components/ui/Banner";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { Input } from "@/components/ui/Input";
import { Menu, MenuContent, MenuItem, MenuLabel, MenuTrigger } from "@/components/ui/Menu";
import { StatusDot } from "@/components/ui/StatusDot";
import { FieldLabel } from "@/help/HelpButton";
import { saveFormOnShortcut } from "@/lib/keyboard";

import { useCapabilities, useTestDevice, useUpdateDevice } from "./api";
import { CapabilitiesPanel } from "./Capabilities";
import { diffConfig, type ConflictRow } from "./conflict";
import { ConflictDialog } from "./ConflictDialog";
import { DeviceForm } from "./DeviceForm";
import { ChangeDriverSheet } from "./RemapSheet";
import { TestResult } from "./TestResult";
import { transportLabel } from "./transports";
import type { Device, DeviceConfig, DeviceStatusValue, Driver, TestReport } from "./types";
import { useDeviceForm } from "./useDeviceForm";

function summaryLine(device: Device): string {
  const transport = (device.config.transport ?? {}) as Record<string, unknown>;
  const parts: string[] = [];
  const host = transport["host"];
  const port = transport["port"];
  const path = transport["device_path"];
  if (typeof host === "string" && host) parts.push(typeof port === "number" ? `${host}:${port}` : host);
  else if (typeof path === "string" && path) parts.push(path);
  else if (typeof transport["type"] === "string") parts.push(transportLabel(transport["type"] as string));
  const detail = device.status?.detail;
  if (detail) parts.push(detail);
  return parts.join(" · ");
}

export interface DeviceCardProps {
  device: Device;
  driver: Driver | undefined;
  /** Every driver in this device's category, for the change-driver menu. */
  alternatives: readonly Driver[];
}

export function DeviceCard({ device, driver, alternatives }: DeviceCardProps) {
  const form = useDeviceForm(driver, device.config);
  const update = useUpdateDevice();
  const test = useTestDevice();
  const capabilities = useCapabilities(device.id);
  const [name, setName] = useState(device.name);
  const [report, setReport] = useState<TestReport | undefined>();
  const [testFailure, setTestFailure] = useState<string | undefined>();
  const [saved, setSaved] = useState(false);
  const [reverted, setReverted] = useState<string | undefined>();
  const [conflict, setConflict] = useState<{ rows: ConflictRow[]; version: string } | null>(null);
  // The change-driver sheet: `target` null opens it straight at re-mapping.
  // `seq` remounts it fresh each time it opens.
  const [changing, setChanging] = useState<{ target: Driver | null; seq: number } | null>(null);
  const [changed, setChanged] = useState<string | undefined>();
  const pendingConfig = useRef<DeviceConfig | null>(null);

  const status: DeviceStatusValue = device.status?.status ?? "unconfigured";
  // The categories whose rows hold a driver reference (§15.6, §15.10).
  const holdsReferences = device.category === "mixer" || device.category === "video_matrix";

  function openChange(target: Driver | null) {
    setChanged(undefined);
    setChanging((current) => ({ target, seq: (current?.seq ?? 0) + 1 }));
  }
  const idPrefix = `device-${device.id}`;
  const connections = device.status?.connections ?? [];

  function save(version: string) {
    const config = form.buildConfig();
    pendingConfig.current = config;
    setSaved(false);
    setReverted(undefined);
    update.mutate(
      { id: device.id, version, name, config },
      {
        onSuccess: (updated) => {
          form.reset(updated.config);
          setSaved(true);
          setConflict(null);
        },
        onError: (error) => {
          if (!(error instanceof ApiError)) {
            presentError(error);
            return;
          }
          if (error.code === "conflict") {
            const current = error.detail["current"] as Device | undefined;
            setConflict({
              rows: current ? diffConfig(current.config, config) : [],
              version: current?.updated_at ?? version,
            });
            return;
          }
          if (error.code === "device_unavailable" && error.detail["reverted"] === true) {
            const restored = error.detail["device"] as Device | undefined;
            if (restored) form.reset(restored.config);
            const reason = typeof error.detail["reason"] === "string" ? error.detail["reason"] : error.message;
            setReverted(reason);
            return;
          }
          if (error.code === "validation_failed") {
            const presentation = presentError(error);
            if (presentation?.kind === "inline") form.setErrors(presentation.fields);
            return;
          }
          presentError(error);
        },
      },
    );
  }

  function onSubmit() {
    const errors = form.validateAll();
    if (Object.keys(errors).length > 0) return;
    save(device.updated_at);
  }

  function runTest() {
    setReport(undefined);
    setTestFailure(undefined);
    test.mutate(device.id, {
      onSuccess: (result) => setReport(result),
      onError: (error) => {
        const message =
          error instanceof ApiError ? error.message : "The test could not be run — the controller did not answer.";
        setTestFailure(message);
      },
    });
  }

  const capabilityError = capabilities.error;
  const unavailable =
    capabilityError instanceof ApiError ? capabilityError.message : capabilityError ? "The device could not be reached." : undefined;

  return (
    <Card className="device-card" data-device={device.id} data-status={status}>
      <header className="device-head">
        <div className="device-title">
          <StatusDot status={status} subject={device.name} size="lg" />
          <h3 className="card-title">{driver?.name ?? device.driver_key}</h3>
          <span className="pill technical">{device.driver_key}</span>
        </div>
        <div className="device-head-actions">
          {holdsReferences ? (
            <Button variant="secondary" onClick={() => openChange(null)}>
              Re-map references
            </Button>
          ) : null}
          {alternatives.length > 1 ? (
            <Menu>
              <MenuTrigger asChild>
                <Button variant="secondary">Change driver</Button>
              </MenuTrigger>
              <MenuContent align="end">
                <MenuLabel>Replace {driver?.name ?? device.driver_key} with</MenuLabel>
                {alternatives
                  .filter((candidate) => candidate.key !== device.driver_key)
                  .map((candidate) => (
                    <MenuItem key={candidate.key} onSelect={() => openChange(candidate)}>
                      {candidate.name}
                    </MenuItem>
                  ))}
              </MenuContent>
            </Menu>
          ) : null}
        </div>
      </header>
      {changed ? <Banner tone="success">{changed}</Banner> : null}
      <p className="device-sub technical">{summaryLine(device)}</p>

      {connections.length > 0 ? (
        <section className="device-section" aria-labelledby={`${idPrefix}-connections`}>
          <h4 className="sect-label" id={`${idPrefix}-connections`}>
            Connections
          </h4>
          <ul className="connection-list">
            {connections.map((connection) => (
              <li className="connection-row" key={connection.name}>
                <StatusDot status={connection.status} subject={connection.name} />
                <span className="connection-name">{connection.name}</span>
                <span className="technical">{connection.endpoint ?? ""}</span>
                <span className="connection-purpose">{connection.detail ?? connection.purpose ?? ""}</span>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      <form
        className="device-body"
        noValidate
        onSubmit={(event) => {
          event.preventDefault();
          onSubmit();
        }}
        onKeyDown={saveFormOnShortcut}
      >
        <div className="field schema-field">
          <FieldLabel htmlFor={`${idPrefix}-name`} help="devices.name">
            Name
          </FieldLabel>
          <Input id={`${idPrefix}-name`} value={name} onChange={(event) => setName(event.currentTarget.value)} />
        </div>

        {driver ? (
          <DeviceForm driver={driver} form={form} idPrefix={idPrefix} disabled={update.isPending} />
        ) : (
          <Banner tone="danger" title="This driver does not ship with this version">
            The stored configuration names {device.driver_key}, which is not registered. Change the driver to one that
            ships, or restore a backup taken with the version that had it.
          </Banner>
        )}

        <section className="device-section" aria-labelledby={`${idPrefix}-capabilities`}>
          <h4 className="sect-label" id={`${idPrefix}-capabilities`}>
            Capabilities
          </h4>
          <CapabilitiesPanel
            capabilities={capabilities.data?.capabilities}
            asConnected={capabilities.data?.as_connected ?? false}
            declared={driver?.capabilities}
            loading={capabilities.isPending}
            unavailable={unavailable}
          />
        </section>

        {reverted ? (
          <Banner tone="danger" title="Saved settings could not reach the device, so they were undone">
            {reverted} The previous settings have been restored and the device is running on them.
          </Banner>
        ) : null}
        {saved ? <Banner tone="success" title="Saved">The device reconnected with the new settings.</Banner> : null}

        <div className="device-actions">
          <Button type="submit" variant="primary" helpId="devices.save" loading={update.isPending}>
            Save
          </Button>
          <Button
            variant="secondary"
            disabled={!form.dirty && name === device.name}
            onClick={() => {
              form.reset(device.config);
              setName(device.name);
              setSaved(false);
              setReverted(undefined);
            }}
          >
            Discard changes
          </Button>
          <Button variant="secondary" onClick={runTest} loading={test.isPending}>
            Test connection
          </Button>
        </div>
      </form>

      <TestResult report={report} pending={test.isPending} failure={testFailure} />

      <ConflictDialog
        open={conflict !== null}
        onOpenChange={(open) => {
          if (!open) setConflict(null);
        }}
        deviceName={device.name}
        rows={conflict?.rows ?? []}
        onReload={() => {
          form.reset(device.config);
          setName(device.name);
          setConflict(null);
        }}
        onOverwrite={() => {
          const version = conflict?.version ?? device.updated_at;
          setConflict(null);
          save(version);
        }}
      />

      {changing ? (
        <ChangeDriverSheet
          key={changing.seq}
          open
          onOpenChange={(open) => {
            if (!open) setChanging(null);
          }}
          device={device}
          target={changing.target}
          onFinished={setChanged}
        />
      ) : null}
    </Card>
  );
}
