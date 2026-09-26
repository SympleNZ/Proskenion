/*
 * The first-run wizard (spec §10.4).
 *
 * The only bootstrap path, and it is resumable by construction: each step
 * writes its result on the controller, so an abandoned setup reopens at the
 * first incomplete step and every completed step shows a summary with an edit
 * option. Closing the browser loses nothing that was submitted.
 */
import { Check, Lock } from "lucide-react";
import { useState } from "react";
import { useNavigate } from "react-router-dom";

import {
  CERTIFICATE_REFUSED_MESSAGE,
  getCertificateChange,
  noteCertificateChange,
  useCertificateChange,
} from "@/api/certificateChange";
import { ApiError, NetworkError } from "@/api/client";
import { presentationFor } from "@/api/errors";
import { CertificateChangedBanner } from "@/components/CertificateChangedBanner";
import { Banner } from "@/components/ui/Banner";
import { Button } from "@/components/ui/Button";
import { ErrorState, Skeleton } from "@/components/ui/EmptyState";

import { useCompleteSetup, useSetupState, useSubmitStep } from "./api";
import { summaryText } from "./summaries";
import { CertificateStep, DevicesStep, NetworkStep, PasswordStep, ReviewStep, WelcomeStep, type StepProps } from "./steps";
import { STEP_COUNT, type SetupStep } from "./types";

interface Refusal {
  message: string;
  fields: Record<string, string>;
}

function refusalOf(error: unknown): Refusal | null {
  if (error instanceof NetworkError) {
    // Right after the certificate step replaced the certificate, a request
    // that never arrived was refused by the browser at TLS, not lost.
    const message = getCertificateChange() ? CERTIFICATE_REFUSED_MESSAGE : error.message;
    return { message, fields: {} };
  }
  if (!(error instanceof ApiError)) return error ? { message: "Something went wrong", fields: {} } : null;
  const presentation = presentationFor(error);
  if (presentation.kind === "inline") return { message: presentation.message, fields: presentation.fields };
  if (presentation.kind === "countdown") {
    return {
      message: `${presentation.message} Try again in ${presentation.retryAfter} seconds.`,
      fields: {},
    };
  }
  return { message: presentation.message, fields: {} };
}

function StepList({
  steps,
  current,
  onSelect,
}: {
  steps: SetupStep[];
  current: number;
  onSelect: (step: number) => void;
}) {
  return (
    <ol className="wizard-steps" aria-label="Setup steps">
      {steps.map((step) => {
        return (
          <li className="wizard-step" key={step.key} data-state={step.completed ? "complete" : step.step === current ? "current" : "pending"}>
            <span className="wizard-step-mark" aria-hidden="true">
              {step.completed ? <Check className="size-4" strokeWidth={3} /> : step.step}
            </span>
            <span className="wizard-step-body">
              <span className="wizard-step-label">
                {step.label}
                <span className="sr-only">
                  {step.completed ? " — complete" : step.step === current ? " — current step" : " — not done"}
                </span>
              </span>
              {step.completed ? <span className="wizard-step-summary">{summaryText(step)}</span> : null}
            </span>
            {step.completed && step.step !== current ? (
              <Button variant="ghost" onClick={() => onSelect(step.step)} aria-label={`Edit ${step.label}`}>
                Edit
              </Button>
            ) : null}
          </li>
        );
      })}
    </ol>
  );
}

function Body({
  current,
  props,
  onCommit,
  committing,
  onEdit,
}: {
  current: number;
  props: StepProps;
  onCommit: () => void;
  committing: boolean;
  onEdit: (step: number) => void;
}) {
  switch (current) {
    case 1:
      return <WelcomeStep {...props} />;
    case 2:
      return <PasswordStep {...props} who="admin" />;
    case 3:
      return <NetworkStep {...props} />;
    case 4:
      return <DevicesStep {...props} />;
    case 5:
      return <PasswordStep {...props} who="operator" />;
    case 6:
      return <CertificateStep {...props} />;
    default:
      return <ReviewStep {...props} onCommit={onCommit} committing={committing} onEdit={onEdit} />;
  }
}

export function SetupWizard() {
  const navigate = useNavigate();
  const query = useSetupState();
  const submit = useSubmitStep();
  const complete = useCompleteSetup();
  const [current, setCurrent] = useState<number | null>(null);
  const [committed, setCommitted] = useState(false);
  const certificateChange = useCertificateChange();

  if (query.isPending) {
    return (
      <main className="wizard" aria-busy="true" aria-label="Loading first-run setup">
        <Skeleton className="h-touch-primary w-full" />
        <Skeleton className="h-touch w-full" />
      </main>
    );
  }

  if (query.isError) {
    const error = query.error;
    if (error instanceof ApiError && error.reason === "setup_complete") {
      return (
        <main className="wizard centre">
          <ErrorState
            title="This controller is already set up"
            detail={error.message}
            onBack={() => navigate("/login", { replace: true })}
          />
        </main>
      );
    }
    return (
      <main className="wizard centre">
        <ErrorState
          title="Could not load first-run setup"
          detail="The controller did not answer. Nothing has been changed."
          status={error instanceof ApiError ? `${error.status} ${error.code}` : undefined}
          onRetry={() => void query.refetch()}
        />
      </main>
    );
  }

  const state = query.data;
  // Resumability (§10.4): the wizard opens at the first incomplete step until
  // something is chosen, so this is derived rather than copied into state.
  const step = current ?? state.next_step ?? STEP_COUNT;
  const refusal = refusalOf(submit.error ?? complete.error);
  const currentStep = state.steps.find((candidate) => candidate.step === step);

  const props: StepProps = {
    state,
    pending: submit.isPending,
    errors: refusal?.fields ?? {},
    message: refusal?.message,
    submit: (body) =>
      submit.mutate(
        { step, body },
        {
          onSuccess: (response) => {
            if (response.certificate_replaced) noteCertificateChange(response.certificate_names ?? []);
            setCurrent(response.next_step ?? STEP_COUNT);
          },
        },
      ),
  };

  return (
    <main className="wizard">
      <header className="wizard-head">
        <div>
          <h1 className="view-title">First-run setup</h1>
          <p className="view-lede">
            Step {step} of {STEP_COUNT}
            {currentStep ? ` — ${currentStep.label}` : ""}. Everything submitted is kept, so this can be finished later
            from where it stopped.
          </p>
        </div>
        <span className="wizard-lock" aria-hidden="true">
          <Lock className="size-5" />
        </span>
      </header>

      {certificateChange ? <CertificateChangedBanner change={certificateChange} /> : null}

      {committed ? (
        <Banner tone="success" title="Setup complete">
          The controller has left first-run mode. Opening the operator view.
        </Banner>
      ) : null}

      <div className="wizard-layout">
        <nav className="wizard-nav" aria-label="Setup progress">
          <StepList steps={state.steps} current={step} onSelect={(target) => setCurrent(target)} />
        </nav>
        <section className="wizard-panel" aria-labelledby="wizard-step-title">
          <h2 className="section-title" id="wizard-step-title">
            {currentStep?.label ?? "Summary and commit"}
          </h2>
          {currentStep?.completed && step !== STEP_COUNT ? (
            <Banner tone="info" title="This step is already done">
              {summaryText(currentStep)}. Submitting it again replaces what was recorded.
            </Banner>
          ) : null}
          <Body
            current={step}
            props={props}
            committing={complete.isPending}
            onEdit={(target) => setCurrent(target)}
            onCommit={() =>
              complete.mutate(undefined, {
                onSuccess: () => {
                  setCommitted(true);
                  navigate("/app", { replace: true });
                },
              })
            }
          />
        </section>
      </div>
    </main>
  );
}
