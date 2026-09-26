/*
 * The operator Projector view (spec §21.14, §7.4). Power, an input selector,
 * and a state line whose LED carries all six of §7.4's states. During
 * WARMING or COOLING every control is disabled and the state line gives the
 * reason — an earlier revision queued the command instead and produced
 * delayed surprise (§7.4); rejection with a stated reason is the honest
 * version, and a scene is how "turn it on once it has cooled" is expressed
 * (§8.13).
 *
 * `state` and `input_ref` are live (§21.2): once the socket is open they are
 * overtaken by the `projector_state` frame, which fires on every transition.
 * `inputs` and `remaining_s` travel only with the REST response, because the
 * frame does not carry them — the countdown shown here is a local clock
 * seeded from the last response, not a per-second server push.
 */
import { Circle, Loader2, Projector as ProjectorIcon, X } from "lucide-react";
import { useEffect } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { ApiError } from "@/api/client";
import { presentError } from "@/api/errors";
import { useCountdown } from "@/components/useCountdown";
import { Button } from "@/components/ui/Button";
import { EmptyState, ErrorState, Skeleton } from "@/components/ui/EmptyState";
import { Select } from "@/components/ui/Select";
import { setProjectorState, useProjectorState as useLiveProjectorState } from "@/live/store";

import { projectorKeys, useProjectorState, useSetProjectorInput, useSetProjectorPower } from "./api";
import type { ProjectorPowerState, ProjectorStateResponse } from "./types";

function statusLine(error: unknown): string | undefined {
  return error instanceof ApiError ? `${error.status} ${error.code}` : undefined;
}

const KNOWN_STATES: ReadonlySet<string> = new Set<ProjectorPowerState>([
  "off",
  "warming",
  "on",
  "cooling",
  "error",
  "unreachable",
]);

function asPowerState(value: string | null | undefined): ProjectorPowerState | null {
  return value != null && KNOWN_STATES.has(value) ? (value as ProjectorPowerState) : null;
}

const STATE_LABELS: Readonly<Record<ProjectorPowerState, string>> = {
  off: "Off",
  warming: "Warming up",
  on: "On",
  cooling: "Cooling down",
  error: "Error",
  unreachable: "Unreachable",
};

type LedIcon = "filled" | "spinner" | "hollow" | "cross";

const STATE_ICONS: Readonly<Record<ProjectorPowerState, LedIcon>> = {
  off: "hollow",
  warming: "spinner",
  on: "filled",
  cooling: "spinner",
  error: "cross",
  unreachable: "cross",
};

/** Colour and icon together, never colour alone (§24.1). */
function PowerLed({ state }: { state: ProjectorPowerState }) {
  const icon = STATE_ICONS[state];
  return (
    <span className="projector-led" data-state={state} aria-hidden="true">
      {icon === "hollow" && <Circle strokeWidth={2.5} />}
      {icon === "spinner" && <Loader2 strokeWidth={3} />}
      {icon === "cross" && <X strokeWidth={3} />}
    </span>
  );
}

export function ProjectorView() {
  const query = useProjectorState();
  const live = useLiveProjectorState();
  const power = useSetProjectorPower();
  const input = useSetProjectorInput();
  const queryClient = useQueryClient();
  const countdown = useCountdown();

  const data = query.data;
  // A frame, once one has arrived, is trusted over the REST snapshot — even
  // when it reports a null input, which is a real "nothing selected" rather
  // than an absence to fall through past.
  const state = live ? asPowerState(live.state) : (data?.state ?? null);
  const inputRef = live ? live.inputRef : (data?.input_ref ?? null);
  const remainingS = data?.remaining_s ?? null;
  const transitioning = state === "warming" || state === "cooling";

  useEffect(() => {
    if (transitioning && remainingS !== null) countdown.start(remainingS);
    else countdown.clear();
    // countdown.start/clear are stable (useCountdown's useCallback has no deps).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [transitioning, remainingS]);

  function applyDetailState(error: unknown): void {
    if (!(error instanceof ApiError) || error.code !== "device_unavailable") return;
    const detailState = asPowerState(typeof error.detail["state"] === "string" ? error.detail["state"] : null);
    if (detailState) setProjectorState({ state: detailState, inputRef });
  }

  function onSuccess(response: ProjectorStateResponse): void {
    queryClient.setQueryData<ProjectorStateResponse>(projectorKeys.state, response);
    if (response.state) setProjectorState({ state: response.state, inputRef: response.input_ref });
  }

  function handlePower(on: boolean): void {
    power.mutate(on, {
      onSuccess,
      onError: (error) => {
        applyDetailState(error);
        presentError(error);
      },
    });
  }

  function handleInput(ref: string): void {
    input.mutate(ref, {
      onSuccess,
      onError: (error) => {
        applyDetailState(error);
        presentError(error);
      },
    });
  }

  if (query.isPending) {
    return (
      <div className="projector-view" aria-busy="true" aria-label="Loading the projector">
        <h1 className="sr-only">Projector</h1>
        <Skeleton className="h-touch-primary w-full" />
      </div>
    );
  }

  if (query.isError) {
    return (
      <div className="projector-view">
        <h1 className="view-title">Projector</h1>
        <ErrorState
          title="Could not load the projector"
          detail="The controller did not answer. Nothing has been changed."
          status={statusLine(query.error)}
          onRetry={() => void query.refetch()}
        />
      </div>
    );
  }

  if (!data || data.device_id === null) {
    return (
      <div className="projector-view">
        <h1 className="view-title">Projector</h1>
        <EmptyState icon={ProjectorIcon} title="No projector configured" detail="An admin configures the projector in Devices." />
      </div>
    );
  }

  const disabled = transitioning || power.isPending || input.isPending;
  const countdownSuffix = transitioning && countdown.active ? ` — ${countdown.remaining} s` : "";
  // A configured projector always carries a state per the contract; this
  // guards only against a not-yet-recognised value reaching the client.
  const stateLabel = state ? STATE_LABELS[state] : "Unknown";

  return (
    <div className="projector-view">
      <h1 className="view-title">Projector</h1>
      <p className="projector-state-line" role="status" aria-live="polite">
        {state ? <PowerLed state={state} /> : <span className="projector-led" data-state="unknown" aria-hidden="true" />}
        {`Projector: ${stateLabel}${countdownSuffix}`}
      </p>
      <div className="projector-power-row">
        <Button variant="primary" size="primary" disabled={disabled || state === "on"} onClick={() => handlePower(true)}>
          Turn on
        </Button>
        <Button variant="secondary" size="primary" disabled={disabled || state === "off"} onClick={() => handlePower(false)}>
          Turn off
        </Button>
      </div>
      <label className="projector-input-picker">
        <span>Input</span>
        <Select
          value={inputRef ?? ""}
          disabled={disabled || data.inputs.length === 0}
          onChange={(event) => handleInput(event.target.value)}
        >
          <option value="" disabled>
            Choose an input
          </option>
          {data.inputs.map((option) => (
            <option key={option.ref} value={option.ref}>
              {option.label}
            </option>
          ))}
        </Select>
      </label>
    </div>
  );
}
