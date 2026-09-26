/*
 * §21.24 *Backup*'s "Backup media" block: local, the USB stick and one
 * network destination (SMB or SFTP), each with its state — reachable, absent
 * media, last success — read from the last attempt (`BackupStatus.last_run`)
 * rather than only the saved configuration, because a destination that is
 * configured but unreachable is exactly what this card exists to surface.
 *
 * Credentials are write-only (§6.10's convention, the same shape
 * `admin/certs/CertificatesScreen.tsx` uses for the Cloudflare token):
 * `GET` answers `password_set`, never the password, and an SFTP destination
 * has no password field at all — it authenticates with the key pair this
 * device generated, whose public half is shown here with a copy action.
 */
import { useState } from "react";
import { Check, Copy } from "lucide-react";

import { ApiError } from "@/api/client";
import { Banner } from "@/components/ui/Banner";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { Field, Input } from "@/components/ui/Input";
import { LevelDot } from "@/components/ui/LevelDot";
import { Checkbox, Select } from "@/components/ui/Select";
import type { Level } from "@/components/ui/levels";

import { useBackupDestinations, useSaveDestination, useSftpPublicKey } from "./api";
import type { BackupStatus, NetworkDestinationUpdate, NetworkProtocol } from "./types";

function destinationLevel(configured: boolean, present: boolean, ok: boolean | null | undefined): Level {
  if (!configured) return "unknown";
  if (!present) return "amber"; // absent media reads as a warning, not a failure of the destination itself
  if (ok === false) return "red";
  if (ok === true) return "green";
  return "unknown"; // configured, present, never yet attempted
}

interface DestinationRowProps {
  label: string;
  configured: boolean;
  present: boolean;
  ok: boolean | null | undefined;
  reason: string | null | undefined;
  detail: string;
}

function DestinationRow({ label, configured, present, ok, reason, detail }: DestinationRowProps) {
  const level = destinationLevel(configured, present, ok);
  return (
    <div className="flex items-center gap-3">
      <LevelDot level={level} subject={label} />
      <span className="font-medium">{label}</span>
      <span className="text-fg-muted text-sm">{detail}</span>
      {reason ? <span className={level === "red" ? "text-danger-text text-sm" : "text-fg-muted text-sm"}>{reason}</span> : null}
    </div>
  );
}

function statusLine(error: unknown): string | undefined {
  return error instanceof ApiError ? `${error.status} ${error.code}` : undefined;
}

export interface DestinationsCardProps {
  status: BackupStatus | undefined;
}

export function DestinationsCard({ status }: DestinationsCardProps) {
  const destinations = useBackupDestinations();
  const save = useSaveDestination();

  const [editing, setEditing] = useState(false);
  const [protocol, setProtocol] = useState<NetworkProtocol | "none">("none");
  const [host, setHost] = useState("");
  const [port, setPort] = useState("");
  const [path, setPath] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [enabled, setEnabled] = useState(true);
  const [saveError, setSaveError] = useState<string | undefined>();
  const [copied, setCopied] = useState(false);

  const network = destinations.data?.network;
  const sftpKey = useSftpPublicKey(!editing && network?.protocol === "sftp");

  function startEditing(): void {
    setProtocol(network?.protocol ?? "none");
    setHost(network?.host ?? "");
    setPort(network?.port != null ? String(network.port) : "");
    setPath(network?.path ?? "");
    setUsername(network?.username ?? "");
    setPassword("");
    setEnabled(network?.enabled ?? true);
    setSaveError(undefined);
    setEditing(true);
  }

  function doSave(): void {
    const body: NetworkDestinationUpdate = {
      protocol: protocol === "none" ? null : protocol,
      host: protocol === "none" ? null : host.trim(),
      port: protocol === "none" || !port ? null : Number(port),
      path: protocol === "none" ? null : path.trim(),
      username: protocol === "none" ? null : username.trim(),
      enabled: protocol !== "none" && enabled,
    };
    if (password) body.password = password;
    save.mutate(body, {
      onSuccess: () => setEditing(false),
      onError: (error) => setSaveError(error instanceof ApiError ? error.message : "Could not save the destination"),
    });
  }

  async function copyKey(): Promise<void> {
    if (!sftpKey.data) return;
    try {
      await navigator.clipboard.writeText(sftpKey.data);
      setCopied(true);
      setTimeout(() => setCopied(false), 3000);
    } catch {
      // Clipboard access can be refused; the key is still selectable text below.
    }
  }

  if (destinations.isPending) {
    return (
      <Card className="device-card" title="Backup destinations" titleLevel="h2">
        <p className="text-fg-muted text-sm">Loading…</p>
      </Card>
    );
  }

  if (destinations.isError || !destinations.data) {
    return (
      <Card className="device-card" title="Backup destinations" titleLevel="h2">
        <Banner tone="danger">Could not load the backup destinations. {statusLine(destinations.error)}</Banner>
      </Card>
    );
  }

  const { local, usb } = destinations.data;
  const runDestinations = status?.last_run?.destinations;

  return (
    <Card className="device-card" title="Backup destinations" titleLevel="h2">
      <div className="flex flex-col gap-3">
        <DestinationRow
          label="Local"
          configured
          present
          ok={runDestinations?.["local"]?.ok}
          reason={runDestinations?.["local"]?.reason}
          detail={`${local.path} · ${local.retention_days} days retained`}
        />
        <DestinationRow
          label="Backup USB"
          configured
          present={usb.present}
          ok={runDestinations?.["usb"]?.ok}
          reason={usb.present ? runDestinations?.["usb"]?.reason : "Backup media not detected"}
          detail={usb.present ? `${usb.path} · ${usb.retention_days} days retained` : `${usb.retention_days} days retained when present`}
        />
        <DestinationRow
          label="Network"
          configured={network?.protocol != null && network.enabled}
          present
          ok={runDestinations?.["network"]?.ok}
          reason={runDestinations?.["network"]?.reason}
          detail={
            network?.protocol != null
              ? `${network.host} (${network.protocol.toUpperCase()}) · ${network.retention_days} days retained${network.enabled ? "" : " · disabled"}`
              : "Not configured"
          }
        />
      </div>

      {editing ? (
        <div className="flex flex-col gap-3 border-t border-line pt-3">
          <Field label="Protocol" htmlFor="backup-dest-protocol" helpId="backup.destination.protocol" errorId="backup-dest-protocol-error">
            <Select
              id="backup-dest-protocol"
              value={protocol}
              onChange={(e) => setProtocol(e.currentTarget.value as NetworkProtocol | "none")}
            >
              <option value="none">None</option>
              <option value="smb">SMB</option>
              <option value="sftp">SFTP</option>
            </Select>
          </Field>

          {protocol !== "none" ? (
            <>
              <div className="grid grid-cols-2 gap-4">
                <Field label="Host" htmlFor="backup-dest-host" helpId="backup.destination.host" errorId="backup-dest-host-error">
                  <Input id="backup-dest-host" mono value={host} onChange={(e) => setHost(e.currentTarget.value)} />
                </Field>
                <Field label="Port" htmlFor="backup-dest-port" helpId="backup.destination.port" errorId="backup-dest-port-error">
                  <Input
                    id="backup-dest-port"
                    type="number"
                    mono
                    value={port}
                    placeholder={protocol === "smb" ? "445" : "22"}
                    onChange={(e) => setPort(e.currentTarget.value)}
                  />
                </Field>
              </div>
              <Field label="Share or path" htmlFor="backup-dest-path" helpId="backup.destination.path" errorId="backup-dest-path-error">
                <Input id="backup-dest-path" mono value={path} onChange={(e) => setPath(e.currentTarget.value)} />
              </Field>
              <Field label="Username" htmlFor="backup-dest-username" helpId="backup.destination.username" errorId="backup-dest-username-error">
                <Input id="backup-dest-username" value={username} onChange={(e) => setUsername(e.currentTarget.value)} />
              </Field>
              {protocol === "smb" ? (
                <Field label="Password" htmlFor="backup-dest-password" helpId="backup.destination.password" errorId="backup-dest-password-error">
                  <Input
                    id="backup-dest-password"
                    type="password"
                    autoComplete="off"
                    value={password}
                    placeholder={network?.password_set ? "Leave blank to keep the stored password" : ""}
                    onChange={(e) => setPassword(e.currentTarget.value)}
                  />
                </Field>
              ) : (
                <p className="field-help">
                  SFTP authenticates with this device&apos;s own key pair, not a password — install the public key
                  below on the NAS.
                </p>
              )}
              <Checkbox id="backup-dest-enabled" label="Back up to this destination" checked={enabled} onChange={(e) => setEnabled(e.currentTarget.checked)} />
            </>
          ) : null}

          {saveError ? (
            <p className="field-note" role="alert">
              {saveError}
            </p>
          ) : null}

          <div className="flex gap-3">
            <Button variant="primary" helpId="backup.destination.save" loading={save.isPending} onClick={doSave}>
              Save
            </Button>
            <Button variant="ghost" onClick={() => setEditing(false)}>
              Cancel
            </Button>
          </div>
        </div>
      ) : (
        <div>
          <Button variant="secondary" onClick={startEditing}>
            {network?.protocol ? "Change network destination" : "Set up a network destination"}
          </Button>
        </div>
      )}

      {!editing && network?.protocol === "sftp" ? (
        <div className="flex flex-col gap-2 border-t border-line pt-3">
          <p className="field-label">SFTP public key</p>
          <p className="field-help">Install this on the NAS as an authorised key for the backup account.</p>
          {sftpKey.isPending ? (
            <p className="text-fg-muted text-sm">Loading…</p>
          ) : sftpKey.isError ? (
            <p className="text-danger-text text-sm">Could not read the key.</p>
          ) : (
            <div className="flex items-start gap-2">
              <code className="technical break-all">{sftpKey.data}</code>
              <Button variant="ghost" size="icon" aria-label="Copy the SFTP public key" onClick={() => void copyKey()}>
                {copied ? <Check aria-hidden="true" className="size-4" /> : <Copy aria-hidden="true" className="size-4" />}
              </Button>
            </div>
          )}
        </div>
      ) : null}

      {status?.last_run ? (
        <p className="text-fg-muted text-sm border-t border-line pt-3">
          Last backup {status.last_run.result === "success" ? "succeeded" : "failed"}
          {status.last_run.archive_id ? "" : " — nothing was written"}.
        </p>
      ) : null}
    </Card>
  );
}
