/*
 * Admin → HDMI (§21.22): "A short screen." Inputs, outputs and destinations
 * for the matrix already configured on the Devices screen — this screen maps
 * the driver's physical references onto names the operator sees, and groups
 * outputs into the destinations the operator actually picks from.
 *
 * The matrix's own connection (backend, serial port, baud rate) is device
 * configuration, not HDMI configuration — it lives on Admin → Devices with
 * every other driver instance, the same way the PJLink projector's host and
 * password do. With no matrix device configured, `GET /hdmi/state` answers
 * `device_id: null` and this screen has nothing to map, so it says so and
 * points there (§21.22).
 */
import { MonitorPlay } from "lucide-react";
import { Link } from "react-router-dom";

import { ApiError } from "@/api/client";
import { Banner } from "@/components/ui/Banner";
import { EmptyState, ErrorState, Skeleton } from "@/components/ui/EmptyState";

import { useDeviceRefs, useHdmiDestinations, useHdmiInputs, useHdmiOutputs, useHdmiState } from "./api";
import { DestinationsSection } from "./DestinationsSection";
import { InputsSection } from "./InputsSection";
import { OutputsSection } from "./OutputsSection";

function statusLine(error: unknown): string | undefined {
  return error instanceof ApiError ? `${error.status} ${error.code}` : undefined;
}

export function HdmiScreen() {
  const state = useHdmiState();
  const deviceId = state.data?.device_id ?? null;

  const inputs = useHdmiInputs();
  const outputs = useHdmiOutputs();
  const destinations = useHdmiDestinations();
  const refs = useDeviceRefs(deviceId);

  const loading = state.isPending || (deviceId !== null && (inputs.isPending || outputs.isPending || destinations.isPending || refs.isPending));
  const failed = state.isError || inputs.isError || outputs.isError || destinations.isError || (deviceId !== null && refs.isError);

  return (
    <div className="view hdmi-view">
      <header className="view-head">
        <div>
          <h1 className="view-title">HDMI</h1>
          <p className="view-lede">
            The matrix&apos;s inputs and outputs, named for the operator, and the destinations built from them (§21.22).
          </p>
        </div>
      </header>

      {loading ? (
        <div aria-busy="true" aria-label="Loading the HDMI configuration">
          <Skeleton className="h-touch w-full" />
          <Skeleton className="h-touch w-full" />
        </div>
      ) : failed ? (
        <ErrorState
          title="Could not load the HDMI configuration"
          detail="The controller did not answer. Nothing has been changed."
          status={statusLine(state.error ?? inputs.error ?? outputs.error ?? destinations.error ?? refs.error)}
          onRetry={() => {
            void state.refetch();
            void inputs.refetch();
            void outputs.refetch();
            void destinations.refetch();
            if (deviceId !== null) void refs.refetch();
          }}
        />
      ) : deviceId === null ? (
        <EmptyState
          icon={MonitorPlay}
          title="No HDMI matrix configured"
          detail="Configure the matrix — its backend, serial port and baud rate — on Admin → Devices first. Its inputs, outputs and destinations are named here once it exists."
          action={
            <Link className="btn btn-primary" to="/admin/devices">
              Go to Devices
            </Link>
          }
        />
      ) : (
        <>
          <InputsSection deviceId={deviceId} inputs={inputs.data ?? []} refs={refs.data?.inputs ?? []} />
          <OutputsSection deviceId={deviceId} outputs={outputs.data ?? []} refs={refs.data?.outputs ?? []} destinations={destinations.data ?? []} />
          <DestinationsSection
            deviceId={deviceId}
            destinations={destinations.data ?? []}
            outputs={outputs.data ?? []}
            inputs={inputs.data ?? []}
            supportsAtomicRoute={state.data?.supports_atomic_route ?? false}
          />
          <Banner tone="info">
            This matrix can also be changed from its front panel and its IR remote. Those changes appear here within
            30 seconds but cannot be prevented (§21.22, §7.5).
          </Banner>
        </>
      )}
    </div>
  );
}
