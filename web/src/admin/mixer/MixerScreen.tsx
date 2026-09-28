/*
 * Admin → Mixer (§21.21): "Three tabs" — Main and outputs, Channels, and the
 * desk scene library. The mixer device itself (backend, transport, MIDI
 * host) is configured on Admin → Devices, the same way the HDMI matrix's own
 * connection is (§21.22) — this screen maps what the driver can address onto
 * names the venue uses. With no mixer device configured, `GET /mixer/state`
 * answers `device_id: null` and this screen has nothing to map, so it says
 * so and points there.
 */
import { useId, useState, type KeyboardEvent } from "react";
import { SlidersHorizontal } from "lucide-react";
import { Link } from "react-router-dom";

import { ApiError } from "@/api/client";
import { EmptyState, ErrorState, Skeleton } from "@/components/ui/EmptyState";
import type { FaderLawPoint } from "@/lib/faderLaw";

import { useDeviceRefs, useFaderLaw, useMixerChannels, useMixerDeskScenes, useMixerState } from "./api";
import { ChannelsSection } from "./ChannelsSection";
import { DeskScenesSection } from "./DeskScenesSection";
import { MainPanel } from "./MainPanel";
import { MissingChannelsBanner } from "./MissingChannels";
import { OutputsSection } from "./OutputsSection";
import type { ChannelRef, MixerChannel, MixerDeskScene } from "./types";

function statusLine(error: unknown): string | undefined {
  return error instanceof ApiError ? `${error.status} ${error.code}` : undefined;
}

const TABS = [
  { id: "main-outputs", label: "Main and outputs" },
  { id: "channels", label: "Channels" },
  { id: "desk-scenes", label: "Desk scenes" },
] as const;

type TabId = (typeof TABS)[number]["id"];

export function MixerScreen() {
  const [active, setActive] = useState<TabId>("main-outputs");
  const idPrefix = useId();

  const state = useMixerState();
  const deviceId = state.data?.device_id ?? null;

  const channels = useMixerChannels();
  const deskScenes = useMixerDeskScenes();
  const refs = useDeviceRefs(deviceId);
  const faderLaw = useFaderLaw(deviceId);

  const loading = state.isPending || (deviceId !== null && (channels.isPending || deskScenes.isPending || refs.isPending || faderLaw.isPending));
  const failed = state.isError || channels.isError || deskScenes.isError || (deviceId !== null && (refs.isError || faderLaw.isError));

  function handleKeyDown(event: KeyboardEvent<HTMLDivElement>): void {
    const index = TABS.findIndex((tab) => tab.id === active);
    let next: number;
    if (event.key === "ArrowRight") next = (index + 1) % TABS.length;
    else if (event.key === "ArrowLeft") next = (index - 1 + TABS.length) % TABS.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = TABS.length - 1;
    else return;
    event.preventDefault();
    const target = TABS[next];
    if (!target) return;
    setActive(target.id);
    document.getElementById(`${idPrefix}-tab-${target.id}`)?.focus();
  }

  return (
    <div className="view mixer-view">
      <header className="view-head">
        <div>
          <h1 className="view-title">Mixer</h1>
          <p className="view-lede">
            The CQ-20B&apos;s Main, outputs and input channels, named for the venue, and the desk scene library it recalls (§21.21).
          </p>
        </div>
      </header>

      {loading ? (
        <div aria-busy="true" aria-label="Loading the mixer configuration">
          <Skeleton className="h-touch w-full" />
          <Skeleton className="h-touch w-full" />
        </div>
      ) : failed ? (
        <ErrorState
          title="Could not load the mixer configuration"
          detail="The controller did not answer. Nothing has been changed."
          status={statusLine(state.error ?? channels.error ?? deskScenes.error ?? refs.error ?? faderLaw.error)}
          onRetry={() => {
            void state.refetch();
            void channels.refetch();
            void deskScenes.refetch();
            if (deviceId !== null) {
              void refs.refetch();
              void faderLaw.refetch();
            }
          }}
        />
      ) : deviceId === null ? (
        <EmptyState
          icon={SlidersHorizontal}
          title="No mixer configured"
          detail="Configure the mixer's driver and connection on Admin → Devices first. Its Main, outputs and channels are named here once it exists."
          action={
            <Link className="btn btn-primary" to="/admin/devices">
              Go to Devices
            </Link>
          }
        />
      ) : (
        <>
          <MissingChannelsBanner deviceId={deviceId} />
          <div role="tablist" aria-label="Mixer configuration" className="tab-strip" onKeyDown={handleKeyDown}>
            {TABS.map((tab) => (
              <button
                key={tab.id}
                id={`${idPrefix}-tab-${tab.id}`}
                type="button"
                role="tab"
                className="tab"
                aria-selected={active === tab.id}
                aria-controls={`${idPrefix}-panel-${tab.id}`}
                tabIndex={active === tab.id ? 0 : -1}
                onClick={() => setActive(tab.id)}
              >
                {tab.label}
              </button>
            ))}
          </div>

          <MixerTabs
            deviceId={deviceId}
            active={active}
            idPrefix={idPrefix}
            channels={channels.data ?? []}
            scenes={deskScenes.data ?? []}
            refs={refs.data?.refs ?? []}
            faderLaw={faderLaw.data?.fader_law ?? []}
            recallSupported={state.data?.capabilities.scene_recall ?? false}
          />
        </>
      )}
    </div>
  );
}

interface MixerTabsProps {
  deviceId: number;
  active: TabId;
  idPrefix: string;
  channels: MixerChannel[];
  scenes: MixerDeskScene[];
  refs: ChannelRef[];
  faderLaw: FaderLawPoint[];
  recallSupported: boolean;
}

function MixerTabs({ deviceId, active, idPrefix, channels, scenes, refs, faderLaw, recallSupported }: MixerTabsProps) {
  const main = channels.find((channel) => channel.channel_kind === "main");
  const outputs = channels.filter((channel) => channel.channel_kind === "output");
  const inputs = channels.filter((channel) => channel.channel_kind === "input");

  return (
    <>
      <div id={`${idPrefix}-panel-main-outputs`} role="tabpanel" aria-labelledby={`${idPrefix}-tab-main-outputs`} hidden={active !== "main-outputs"}>
        {active === "main-outputs" ? (
          <div className="flex flex-col gap-6">
            {main ? (
              <MainPanel channel={main} faderLaw={faderLaw} />
            ) : (
              <p className="metric-note" role="alert">
                No Main channel exists yet for this device. It is created automatically once a mixer driver is configured (§7.3).
              </p>
            )}
            <OutputsSection deviceId={deviceId} outputs={outputs} refs={refs} faderLaw={faderLaw} />
          </div>
        ) : null}
      </div>
      <div id={`${idPrefix}-panel-channels`} role="tabpanel" aria-labelledby={`${idPrefix}-tab-channels`} hidden={active !== "channels"}>
        {active === "channels" ? <ChannelsSection deviceId={deviceId} channels={inputs} refs={refs} faderLaw={faderLaw} /> : null}
      </div>
      <div id={`${idPrefix}-panel-desk-scenes`} role="tabpanel" aria-labelledby={`${idPrefix}-tab-desk-scenes`} hidden={active !== "desk-scenes"}>
        {active === "desk-scenes" ? <DeskScenesSection deviceId={deviceId} scenes={scenes} recallSupported={recallSupported} /> : null}
      </div>
    </>
  );
}
