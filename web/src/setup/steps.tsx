/*
 * The seven steps of §10.4.
 *
 * Each step is a small form over one endpoint, so a step that has been
 * submitted is recorded whatever happens next: closing the browser mid-setup
 * loses nothing that was submitted, and the wizard resumes at the first
 * incomplete step.
 */
import { Cpu } from "lucide-react";
import { useState } from "react";

import { ApiError } from "@/api/client";
import { Banner } from "@/components/ui/Banner";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { EmptyState, Skeleton } from "@/components/ui/EmptyState";
import { Input, PasswordField } from "@/components/ui/Input";
import { StatusDot } from "@/components/ui/StatusDot";
import { AddDeviceSheet } from "@/admin/devices/AddDeviceSheet";
import { useDevices, useDrivers, useTestDevice } from "@/admin/devices/api";
import { TestResult } from "@/admin/devices/TestResult";
import { categoryLabel, type TestReport } from "@/admin/devices/types";

import { summaryText } from "./summaries";
import { MIN_PASSWORD_LENGTH, type CertificateState, type Detected, type SetupState, type SetupStep } from "./types";

export interface StepProps {
  state: SetupState;
  submit: (body: Record<string, unknown>) => void;
  pending: boolean;
  /** Per-field messages from the server's `validation_failed` detail (§16.1). */
  errors: Record<string, string>;
  message?: string | undefined;
}

/** A copy of `record` without `id` — clearing a stale test result before a re-test. */
function withoutDevice<T>(record: Record<number, T>, id: number): Record<number, T> {
  return Object.fromEntries(Object.entries(record).filter(([key]) => Number(key) !== id)) as Record<number, T>;
}

function StepError({ message }: { message?: string | undefined }) {
  if (!message) return null;
  return (
    <Banner tone="danger" title="That was not accepted">
      {message}
    </Banner>
  );
}

// -- 1. Welcome ---------------------------------------------------------------------

export function WelcomeStep({ state, submit, pending, message }: StepProps) {
  const detected: Detected = state.detected;
  return (
    <div className="step-body">
      <p>
        This controller has not been set up yet. Seven steps, and nothing here needs a command line. The locale and
        timezone below were inherited from the operating system — confirm them, or correct the locale.
      </p>
      <StepError message={message} />
      <dl className="kv">
        <dt>Platform</dt>
        <dd className="technical">{detected.platform}</dd>
        <dt>Locale</dt>
        <dd className="technical">{detected.locale}</dd>
        <dt>Timezone</dt>
        <dd className="technical">{detected.timezone}</dd>
      </dl>
      <p className="field-help">
        The appliance runs on Pacific/Auckland time throughout, and timestamps carry their offset. It is shown for
        confirmation, not for choosing.
      </p>
      <Button
        variant="primary"
        loading={pending}
        onClick={() => submit({ locale: detected.locale, timezone: detected.timezone })}
      >
        Confirm and continue
      </Button>
    </div>
  );
}

// -- 2 and 5. Passwords -------------------------------------------------------------

export interface PasswordStepProps extends StepProps {
  who: "admin" | "operator";
}

export function PasswordStep({ submit, pending, errors, message, who }: PasswordStepProps) {
  const [password, setPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [local, setLocal] = useState<Record<string, string>>({});

  function onSubmit() {
    const found: Record<string, string> = {};
    if (password.length < MIN_PASSWORD_LENGTH) {
      found["password"] = `The password must be at least ${MIN_PASSWORD_LENGTH} characters.`;
    }
    if (password !== confirmation) found["password_confirm"] = "The two passwords do not match.";
    setLocal(found);
    if (Object.keys(found).length > 0) return;
    submit({ password, password_confirm: confirmation });
  }

  const shown = { ...local, ...errors };

  return (
    <form
      className="step-body"
      noValidate
      onSubmit={(event) => {
        event.preventDefault();
        onSubmit();
      }}
    >
      <p>
        {who === "admin"
          ? "The admin password reaches every configuration screen. At least twelve characters, entered twice."
          : "The operator password is set separately from the admin's. Until this step runs, nobody can sign in as the operator."}
      </p>
      <StepError message={message} />
      <PasswordField
        label={who === "admin" ? "Admin password" : "Operator password"}
        value={password}
        error={shown["password"]}
        autoComplete="new-password"
        onChange={(event) => setPassword(event.currentTarget.value)}
      />
      <PasswordField
        label="Enter it again"
        value={confirmation}
        error={shown["password_confirm"]}
        autoComplete="new-password"
        onChange={(event) => setConfirmation(event.currentTarget.value)}
      />
      <p className="field-help">
        There is no username: one password field decides which tier you are in (§6.3). Choose something the school can
        record somewhere other than this machine.
      </p>
      <Button type="submit" variant="primary" loading={pending}>
        Set the password
      </Button>
    </form>
  );
}

// -- 3. Network ---------------------------------------------------------------------

export function NetworkStep({ state, submit, pending, errors, message }: StepProps) {
  const [address, setAddress] = useState(state.detected.address ?? "");
  const [hostname, setHostname] = useState(state.detected.hostname);

  return (
    <form
      className="step-body"
      noValidate
      onSubmit={(event) => {
        event.preventDefault();
        submit({ address, hostname, skipped: false });
      }}
    >
      <p>
        This is the address the controller answers on now. If it is already correct — a DHCP reservation on the school
        switch is the usual arrangement — skip this step.
      </p>
      <StepError message={message} />
      <div className="field schema-field">
        <label className="field-label" htmlFor="setup-address">
          Address
        </label>
        <Input id="setup-address" mono value={address} onChange={(event) => setAddress(event.currentTarget.value)} />
        <div className="field-error" role="alert" aria-live="assertive">
          {errors["address"] ? <span>{errors["address"]}</span> : null}
        </div>
      </div>
      <div className="field schema-field">
        <label className="field-label" htmlFor="setup-hostname">
          Hostname
        </label>
        <Input id="setup-hostname" mono value={hostname} onChange={(event) => setHostname(event.currentTarget.value)} />
        <p className="field-help">
          The name the certificate is issued for, and the name staff type. A DNS entry and a DHCP reservation are
          recommended; neither is enforced here.
        </p>
        <div className="field-error" role="alert" aria-live="assertive">
          {errors["hostname"] ? <span>{errors["hostname"]}</span> : null}
        </div>
      </div>
      <div className="step-actions">
        <Button type="submit" variant="primary" loading={pending}>
          Record this address
        </Button>
        <Button variant="secondary" loading={pending} onClick={() => submit({ skipped: true })}>
          Skip — the network is already correct
        </Button>
      </div>
    </form>
  );
}

// -- 4. Devices ---------------------------------------------------------------------

/**
 * Uses the same devices API and components as the admin Devices screen
 * (§10.4 step 4, §16.7): per device, its address, port, any device-specific
 * authentication and a test connection button. Reachable from here because
 * step 2 signed the wizard in as admin — the devices routes are admin-tier,
 * not gate-exempt in the sense of being public.
 */
export function DevicesStep({ submit, pending, message }: StepProps) {
  const drivers = useDrivers();
  const devices = useDevices();
  const [adding, setAdding] = useState(false);
  const configured = devices.data?.devices ?? [];

  // One test connection button per device (§10.4 step 4), the same two-stage
  // report the admin Devices screen shows (§5.3). Keyed by device id so
  // testing one row never disturbs another's last result.
  const test = useTestDevice();
  const [reports, setReports] = useState<Record<number, TestReport>>({});
  const [testFailures, setTestFailures] = useState<Record<number, string>>({});

  function runTest(id: number) {
    setReports((current) => withoutDevice(current, id));
    setTestFailures((current) => withoutDevice(current, id));
    test.mutate(id, {
      onSuccess: (result) => setReports((current) => ({ ...current, [id]: result })),
      onError: (error) => {
        const failureMessage =
          error instanceof ApiError ? error.message : "The test could not be run — the controller did not answer.";
        setTestFailures((current) => ({ ...current, [id]: failureMessage }));
      },
    });
  }

  return (
    <div className="step-body">
      <p>
        Per device: its address, its port, any device-specific authentication, and a test. Nothing has to be online —
        the wizard does not require a device to answer, and anything skipped is configured later from Admin → Devices.
      </p>
      <StepError message={message} />
      {devices.isPending || drivers.isPending ? (
        <div aria-busy="true" aria-label="Loading the configured devices">
          <Skeleton className="h-touch w-full" />
        </div>
      ) : configured.length === 0 ? (
        <EmptyState
          icon={Cpu}
          title="No devices configured"
          detail="Add the mixer, the lighting output, the projector and the HDMI matrix now, or skip and add them later."
          action={
            <Button variant="primary" onClick={() => setAdding(true)}>
              Add a device
            </Button>
          }
        />
      ) : (
        <>
          <ul className="setup-device-list">
            {configured.map((device) => {
              const testingThis = test.isPending && test.variables === device.id;
              return (
                <li className="setup-device" key={device.id}>
                  <StatusDot status={device.status?.status ?? "unconfigured"} subject={device.name} />
                  <span>{device.name}</span>
                  <span className="metric-note">{categoryLabel(device.category)}</span>
                  <span className="technical">{device.driver_key}</span>
                  <Button variant="secondary" onClick={() => runTest(device.id)} loading={testingThis}>
                    Test connection
                  </Button>
                  {testingThis || reports[device.id] || testFailures[device.id] ? (
                    <TestResult report={reports[device.id]} pending={testingThis} failure={testFailures[device.id]} />
                  ) : null}
                </li>
              );
            })}
          </ul>
          <Button variant="secondary" onClick={() => setAdding(true)}>
            Add another device
          </Button>
        </>
      )}

      <div className="step-actions">
        <Button
          variant="primary"
          loading={pending}
          onClick={() => submit({ device_ids: configured.map((device) => device.id), skipped: false })}
          disabled={configured.length === 0}
        >
          Continue with {configured.length} configured
        </Button>
        <Button variant="secondary" loading={pending} onClick={() => submit({ device_ids: [], skipped: true })}>
          Skip — configure devices later
        </Button>
      </div>

      <AddDeviceSheet open={adding} onOpenChange={setAdding} drivers={drivers.data?.drivers ?? []} />
    </div>
  );
}

// -- 6. Certificate -----------------------------------------------------------------

export function CertificateStep({ state, submit, pending, errors, message }: StepProps) {
  const certificate: CertificateState = state.certificate;
  const [hostname, setHostname] = useState(certificate.hostname);
  const [chosen, setChosen] = useState(certificate.options.find((option) => option.available)?.id ?? "self_signed");
  const [token, setToken] = useState("");

  return (
    <form
      className="step-body"
      noValidate
      onSubmit={(event) => {
        event.preventDefault();
        submit(chosen === "lets_encrypt" ? { option: chosen, hostname, token } : { option: chosen, hostname });
      }}
    >
      <p>Two ways to get a certificate. One of them is not available if this controller has none configured.</p>
      <StepError message={message} />
      <div className="cert-options">
        {certificate.options.map((option) => (
          <Card
            className="cert-option"
            key={option.id}
            data-available={option.available}
            aria-labelledby={`cert-${option.id}-label`}
          >
            <label className="cert-choice" htmlFor={`cert-${option.id}`}>
              <input
                type="radio"
                id={`cert-${option.id}`}
                name="certificate-option"
                value={option.id}
                checked={chosen === option.id}
                disabled={!option.available}
                aria-describedby={option.reason ? `cert-${option.id}-reason` : undefined}
                onChange={() => setChosen(option.id)}
              />
              <span id={`cert-${option.id}-label`}>{option.label}</span>
            </label>
            {!option.available ? (
              <p className="field-help" id={`cert-${option.id}-reason`}>
                <strong>Not available. </strong>
                {option.reason}
              </p>
            ) : null}
            {option.guidance.length > 0 ? (
              <>
                <h4 className="sect-label">Trusting it on a staff iPad</h4>
                <ol className="guidance-list">
                  {option.guidance.map((line) => (
                    <li key={line}>{line}</li>
                  ))}
                </ol>
              </>
            ) : null}
          </Card>
        ))}
      </div>

      <div className="field schema-field">
        <label className="field-label" htmlFor="setup-cert-hostname">
          Issue it for
        </label>
        <Input
          id="setup-cert-hostname"
          mono
          value={hostname}
          onChange={(event) => setHostname(event.currentTarget.value)}
        />
        <div className="field-error" role="alert" aria-live="assertive">
          {errors["hostname"] ? <span>{errors["hostname"]}</span> : null}
        </div>
      </div>
      {chosen === "lets_encrypt" ? (
        <PasswordField
          id="setup-cert-token"
          label="Cloudflare API token"
          value={token}
          onChange={(event) => setToken(event.currentTarget.value)}
          error={errors["token"]}
          autoComplete="off"
        />
      ) : null}
      {errors["option"] ? (
        <div className="field-error" role="alert" aria-live="assertive">
          <span>{errors["option"]}</span>
        </div>
      ) : null}

      <Button type="submit" variant="primary" loading={pending}>
        {chosen === "lets_encrypt" ? "Issue the certificate" : "Generate the certificate"}
      </Button>
      {chosen === "lets_encrypt" ? (
        <p className="field-help">
          The token is checked first — a rejected token is refused here, with Cloudflare&rsquo;s own reason, so
          nothing is written until it is fixed. If issuance still fails once the token is accepted — no network
          reaching Cloudflare or Let&rsquo;s Encrypt, a DNS problem — a self-signed certificate is generated instead
          so setup is never blocked, and the reason is shown afterwards.
        </p>
      ) : null}
    </form>
  );
}

// -- 7. Summary and commit ----------------------------------------------------------

export interface ReviewStepProps extends StepProps {
  onCommit: () => void;
  committing: boolean;
  onEdit: (step: number) => void;
}

export function ReviewStep({ state, submit, pending, message, onCommit, committing, onEdit }: ReviewStepProps) {
  const configuration: SetupStep[] = state.steps.filter((step) => step.key !== "summary");
  const outstanding = configuration.filter((step) => !step.completed);
  const reviewed = state.steps.find((step) => step.key === "summary")?.completed ?? false;

  return (
    <div className="step-body">
      <p>Everything the wizard has recorded. Committing sets first-run as complete and opens the operator view.</p>
      <StepError message={message} />
      <ul className="review-list">
        {configuration.map((step) => (
          <li className="review-row" key={step.key}>
            <span className="review-step">
              {step.step}. {step.label}
            </span>
            <span className="review-summary">{step.completed ? summaryText(step) : "Not done"}</span>
            <Button variant="ghost" onClick={() => onEdit(step.step)}>
              Edit
            </Button>
          </li>
        ))}
      </ul>
      {outstanding.length > 0 ? (
        <Banner tone="warning" title="Some steps are not complete">
          {outstanding.map((step) => step.label).join(", ")}. Every step from 1 to 6 must be recorded before the wizard
          can commit.
        </Banner>
      ) : null}
      <div className="step-actions">
        {!reviewed ? (
          <Button variant="secondary" loading={pending} onClick={() => submit({ reviewed: true })}>
            Mark reviewed
          </Button>
        ) : null}
        <Button variant="primary" loading={committing} disabled={outstanding.length > 0 || !reviewed} onClick={onCommit}>
          Finish setup
        </Button>
      </div>
      <p className="field-help">
        Re-running the wizard needs a database reset. Individual settings are changed afterwards from their admin pages.
      </p>
    </div>
  );
}
