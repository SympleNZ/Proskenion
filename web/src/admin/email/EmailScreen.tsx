/*
 * Admin → Email (spec §21.24 *Email*, §11.4, §4.6, contracts §5, §7). Host,
 * port, TLS mode, optional credentials, sender and recipient, with a test
 * button that reports inline what failed. Defaults suit Q22's unauthenticated
 * relay on port 25 — `DEFAULT_TLS_MODE` is opportunistic STARTTLS, and
 * `username` stays blank rather than forcing a login.
 */
import { useState } from "react";
import { Mail } from "lucide-react";

import { ApiError } from "@/api/client";
import { presentError, presentationFor } from "@/api/errors";
import { Banner } from "@/components/ui/Banner";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { ErrorState, Skeleton } from "@/components/ui/EmptyState";
import { Field, Input, PasswordField } from "@/components/ui/Input";
import { Select } from "@/components/ui/Select";
import { ConfirmDialog } from "@/components/ui/Sheet";
import { FieldLabel } from "@/help/HelpButton";
import { saveOnShortcut } from "@/lib/keyboard";

import { useDeleteEmailConfig, useEmailConfig, useSaveEmailConfig, useTestEmail } from "./api";
import { stageLabel } from "./stages";
import { DEFAULT_TLS_MODE, TLS_MODES, type EmailConfig, type TlsMode } from "./types";
import { isEmailFormValid, validateEmailForm, type EmailFormErrors, type EmailFormValues } from "./validation";

const TLS_LABELS: Readonly<Record<TlsMode, string>> = {
  none: "None — plain text (an unauthenticated relay on the venue network only)",
  starttls: "Opportunistic — STARTTLS if the relay offers it",
  tls: "TLS — connect already encrypted",
};

function statusLine(error: unknown): string | undefined {
  return error instanceof ApiError ? `${error.status} ${error.code}` : undefined;
}

function formValuesFrom(config: EmailConfig | undefined): EmailFormValues {
  return {
    host: config?.host ?? "",
    port: config?.port != null ? String(config.port) : "25",
    username: config?.username ?? "",
    password: "",
    sender: config?.sender ?? "",
    recipient: config?.recipient ?? "",
  };
}

function serverFieldErrors(error: unknown): EmailFormErrors {
  if (!(error instanceof ApiError) || error.code !== "validation_failed") return {};
  const presentation = presentationFor(error);
  if (presentation.kind !== "inline") return {};
  const out: EmailFormErrors = {};
  for (const [field, message] of Object.entries(presentation.fields)) {
    if (field === "host" || field === "port" || field === "sender" || field === "recipient") out[field] = message;
  }
  return out;
}

export function EmailScreen() {
  const configQuery = useEmailConfig();
  const save = useSaveEmailConfig();
  const test = useTestEmail();
  const remove = useDeleteEmailConfig();

  const [values, setValues] = useState<EmailFormValues>(() => formValuesFrom(undefined));
  const [tlsMode, setTlsMode] = useState<TlsMode>(DEFAULT_TLS_MODE);
  const [synced, setSynced] = useState<string | null | undefined>(undefined);
  const [errors, setErrors] = useState<EmailFormErrors>({});
  const [saveError, setSaveError] = useState<string | undefined>();
  const [testResult, setTestResult] = useState<{ ok: boolean; message: string } | null>(null);
  const [removeConfirmOpen, setRemoveConfirmOpen] = useState(false);

  const config = configQuery.data;
  if (config && config.updated_at !== synced) {
    setSynced(config.updated_at);
    setValues(formValuesFrom(config));
    setTlsMode(config.tls_mode);
  }

  if (configQuery.isPending) {
    return (
      <div className="view email-view" aria-busy="true" aria-label="Loading the email configuration">
        <h1 className="view-title">Email</h1>
        <Skeleton className="h-touch w-full" />
        <Skeleton className="h-touch w-full" />
      </div>
    );
  }

  if (configQuery.isError) {
    return (
      <div className="view email-view">
        <h1 className="view-title">Email</h1>
        <ErrorState
          title="Could not load the email configuration"
          status={statusLine(configQuery.error)}
          onRetry={() => void configQuery.refetch()}
        />
      </div>
    );
  }

  function setField<K extends keyof EmailFormValues>(key: K, value: EmailFormValues[K]): void {
    setValues((prev) => ({ ...prev, [key]: value }));
  }

  function onSave(): void {
    const validation = validateEmailForm(values);
    setErrors(validation);
    setSaveError(undefined);
    if (!isEmailFormValid(validation)) return;
    save.mutate(
      {
        host: values.host.trim(),
        port: Number(values.port),
        tls_mode: tlsMode,
        username: values.username.trim() || null,
        ...(values.password ? { password: values.password } : {}),
        sender: values.sender.trim(),
        recipient: values.recipient.trim(),
      },
      {
        onSuccess: () => {
          setValues((prev) => ({ ...prev, password: "" }));
        },
        onError: (error) => {
          const fieldErrors = serverFieldErrors(error);
          if (Object.keys(fieldErrors).length > 0) {
            setErrors(fieldErrors);
            return;
          }
          if (error instanceof ApiError) {
            setSaveError(error.message);
            return;
          }
          presentError(error);
        },
      },
    );
  }

  function onRemove(): void {
    setRemoveConfirmOpen(false);
    remove.mutate(undefined, {
      onSuccess: () => {
        setTestResult(null);
        setSaveError(undefined);
        setErrors({});
      },
      onError: (error) => {
        setSaveError(error instanceof ApiError ? error.message : "Could not remove the mail settings");
      },
    });
  }

  function onTest(): void {
    setTestResult(null);
    // Whatever is in the form right now, saved or not (system.py's own
    // reasoning: proving an edited-but-unsaved configuration before it
    // commits anything). Blank fields fall back to what is already saved.
    test.mutate(
      {
        ...(values.host.trim() ? { host: values.host.trim() } : {}),
        ...(values.port.trim() ? { port: Number(values.port) } : {}),
        tls_mode: tlsMode,
        ...(values.username.trim() ? { username: values.username.trim() } : {}),
        ...(values.password ? { password: values.password } : {}),
        ...(values.sender.trim() ? { sender: values.sender.trim() } : {}),
        ...(values.recipient.trim() ? { recipient: values.recipient.trim() } : {}),
      },
      {
        onSuccess: (result) => {
          const stage = stageLabel(result.stage);
          setTestResult({ ok: result.ok, message: stage ? `${stage} — ${result.message}` : result.message });
        },
        onError: (error) => {
          setTestResult({ ok: false, message: error instanceof ApiError ? error.message : "Could not run the test" });
        },
      },
    );
  }

  return (
    <div className="view email-view" onKeyDown={saveOnShortcut(onSave)}>
      <header className="view-head">
        <div>
          <h1 className="view-title">Email</h1>
          <p className="view-lede">
            Where alerts are sent (§11.4). Saving also mirrors this to <code className="technical">smtp-fallback.toml</code>{" "}
            so emergency mode can still alert (§4.6).
          </p>
        </div>
      </header>

      {!config?.host ? (
        <Banner tone="info" title="No email configured">
          <Mail aria-hidden="true" className="size-4" /> Alerts have nowhere to go until this is set up.
        </Banner>
      ) : null}

      <Card className="device-card" title="SMTP server" titleLevel="h2">
        <div className="grid grid-cols-2 gap-4">
          <Field label="Host" htmlFor="email-host" helpId="email.host" error={errors.host} errorId="email-host-error">
            <Input
              id="email-host"
              value={values.host}
              onChange={(e) => setField("host", e.currentTarget.value)}
              aria-invalid={errors.host ? true : undefined}
            />
          </Field>
          <Field label="Port" htmlFor="email-port" helpId="email.port" error={errors.port} errorId="email-port-error">
            <Input
              id="email-port"
              type="number"
              min={1}
              max={65535}
              mono
              value={values.port}
              onChange={(e) => setField("port", e.currentTarget.value)}
              aria-invalid={errors.port ? true : undefined}
            />
          </Field>
        </div>

        <div className="field">
          <FieldLabel htmlFor="email-tls-mode" help="email.tls-mode">
            TLS mode
          </FieldLabel>
          <Select id="email-tls-mode" value={tlsMode} onChange={(e) => setTlsMode(e.currentTarget.value as TlsMode)}>
            {TLS_MODES.map((mode) => (
              <option key={mode} value={mode}>
                {TLS_LABELS[mode]}
              </option>
            ))}
          </Select>
        </div>

        <div className="grid grid-cols-2 gap-4">
          <Field label="Username (optional)" htmlFor="email-username" helpId="email.username" error={undefined} errorId="email-username-error">
            <Input id="email-username" value={values.username} onChange={(e) => setField("username", e.currentTarget.value)} />
          </Field>
          <PasswordField
            id="email-password"
            label={config?.password_set ? "Password (set — leave blank to keep it)" : "Password (optional)"}
            helpId="email.password"
            value={values.password}
            onChange={(e) => setField("password", e.currentTarget.value)}
          />
        </div>

        <div className="grid grid-cols-2 gap-4">
          <Field label="Sender" htmlFor="email-sender" helpId="email.sender" error={errors.sender} errorId="email-sender-error">
            <Input
              id="email-sender"
              type="email"
              value={values.sender}
              onChange={(e) => setField("sender", e.currentTarget.value)}
              aria-invalid={errors.sender ? true : undefined}
            />
          </Field>
          <Field label="Recipient" htmlFor="email-recipient" helpId="email.recipient" error={errors.recipient} errorId="email-recipient-error">
            <Input
              id="email-recipient"
              type="email"
              value={values.recipient}
              onChange={(e) => setField("recipient", e.currentTarget.value)}
              aria-invalid={errors.recipient ? true : undefined}
            />
          </Field>
        </div>

        {saveError ? (
          <p className="field-note" role="alert">
            {saveError}
          </p>
        ) : null}

        {testResult ? (
          <p className={testResult.ok ? "text-success-text" : "text-danger-text"} role="status">
            {testResult.message}
          </p>
        ) : null}

        <div className="flex justify-between gap-3">
          {config?.host ? (
            <Button variant="destructive" helpId="email.remove" loading={remove.isPending} onClick={() => setRemoveConfirmOpen(true)}>
              Remove mail settings
            </Button>
          ) : (
            <span />
          )}
          <div className="flex gap-3">
            <Button variant="secondary" loading={test.isPending} onClick={onTest}>
              Send a test email
            </Button>
            <Button variant="primary" helpId="email.save" loading={save.isPending} onClick={onSave}>
              Save
            </Button>
          </div>
        </div>
      </Card>

      <ConfirmDialog
        open={removeConfirmOpen}
        onOpenChange={setRemoveConfirmOpen}
        title="Remove the mail settings?"
        description="Clears the SMTP relay and stops the firewall admitting it (§3.4). Alerts have nowhere to go until this is set up again."
        confirmLabel="Remove mail settings"
        destructive
        onConfirm={onRemove}
      />
    </div>
  );
}
