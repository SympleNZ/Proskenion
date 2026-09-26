/*
 * The operator Mixer view (spec §21.13 — its wireframe is the specification
 * for this view). The pinned Main Output, the outputs drawer, and input
 * pagination, built on the shared `FaderStrip`/`lawFaderScale` and the
 * mixer service's contract (`docs/plans/phase-4-contracts.md`).
 *
 * Wide and narrow share one DOM tree. §21.13 draws two different arrangements
 * — a side drawer that narrows the input area on wide, a full-width page swap
 * on narrow — but the interaction underneath is the same toggle (open or
 * close the outputs panel), and the true visual difference is layout, not
 * structure, so it is left entirely to CSS breakpoints rather than rendered
 * twice. The one piece of narrow's layout this does not attempt to
 * reproduce is hiding Main from the default inputs page (narrow shows Main
 * only once the outputs page is open) — the task brief for this view states
 * "the pinned Main Output, always visible" as its first line of scope, which
 * this follows literally; see the task report for the tension with that
 * specific wireframe detail.
 *
 * Similarly, wide's per-page input count ("calculated from available width
 * with a minimum strip width") is a real layout measurement this
 * view does not fake with an unmeasurable jsdom width; `DEFAULT_PAGE_SIZE`
 * (`pagination.ts`) is used uniformly, and CSS lets the fixed-size page of
 * strips reflow narrower down to that minimum floor rather than scroll.
 */
import { PanelRightClose, PanelRightOpen, SlidersHorizontal } from "lucide-react";
import { useState } from "react";

import { ApiError } from "@/api/client";
import { EmptyState, ErrorState, Skeleton } from "@/components/ui/EmptyState";
import type { FaderLaw } from "@/lib/faderLaw";
import { useMixerMetering } from "@/live/store";

import { useFaderLaw, useMixerState } from "./api";
import { ChannelStrip, PAN_UNAVAILABLE } from "./ChannelStrip";
import { DeskSceneBand } from "./DeskSceneBand";
import { DEFAULT_PAGE_SIZE, pageChipLabel, paginate } from "./pagination";
import type { MeteringReason, MixerCapabilities, MixerOutputChannel, MixerStateResponse } from "./types";

function statusLine(error: unknown): string | undefined {
  return error instanceof ApiError ? `${error.status} ${error.code}` : undefined;
}

/** Narrows the live store's generic `reason: string | null` to the closed vocabulary. */
function asMeteringReason(reason: string | null): MeteringReason {
  return reason === "unsupported" || reason === "refused" || reason === "no_response" ? reason : null;
}

/**
 * The wording is this interface's own; the backend sends only the closed
 * code (`docs/plans/phase-4-contracts.md`). `null` falls back to a
 * bare notice — the backend guarantees `reason` is set whenever `metering`
 * is `false`, so this is defensive rather than a case the interface expects.
 */
function meteringNotice(reason: MeteringReason): string {
  switch (reason) {
    case "unsupported":
      return "Metering unavailable — the driver has no metering.";
    case "refused":
      return "Metering unavailable — the desk refused the metering connection.";
    case "no_response":
      return "Metering unavailable — the desk is not sending meters.";
    default:
      return "Metering unavailable.";
  }
}

interface OutputsPanelProps {
  id: string;
  open: boolean;
  outputs: readonly MixerOutputChannel[];
  law: FaderLaw;
  capabilities: MixerCapabilities;
  connected: boolean;
}

function OutputsPanel({ id, open, outputs, law, capabilities, connected }: OutputsPanelProps) {
  return (
    <div id={id} className="mixer-outputs-panel" data-open={open || undefined} aria-hidden={!open}>
      <h2 className="lighting-section-title">Outputs</h2>
      <div className="mixer-outputs-strips">
        {outputs.map((channel) => (
          <ChannelStrip
            key={channel.channel_id}
            channel={channel}
            target={{ section: "output", id: channel.channel_id }}
            law={law}
            capabilities={capabilities}
            connected={connected}
            testId={`mixer-output-${channel.channel_id}`}
          />
        ))}
      </div>
    </div>
  );
}

interface MainPanelProps {
  main: MixerStateResponse["main"];
  law: FaderLaw;
  capabilities: MixerCapabilities;
  connected: boolean;
}

function MainPanel({ main, law, capabilities, connected }: MainPanelProps) {
  if (!main) return null;
  return (
    <div className="mixer-main-panel">
      <h2 className="lighting-section-title">Main Output</h2>
      <ChannelStrip
        channel={{
          channel_id: main.channel_id,
          name: main.name,
          short_name: main.name,
          stereo: true,
          db: main.db,
          muted: main.muted,
          origin: main.origin,
        }}
        target={{ section: "main", id: main.channel_id }}
        law={law}
        capabilities={capabilities}
        connected={connected}
        faderPosLabel
        testId="mixer-main"
      />
    </div>
  );
}

export function MixerView() {
  const query = useMixerState();
  const deviceId = query.data?.device_id ?? null;
  const lawQuery = useFaderLaw(deviceId);
  // Live, not Query (§21.2, B32): a `mixer_meters` frame's `metering` is
  // what actually reaches an open view on an availability change.
  // `null` until the first such frame this session, in which case
  // `GET /mixer/state`'s own `capabilities.metering`/`metering_reason` — the
  // resync-equivalent source of truth — is the fallback, below.
  const metering = useMixerMetering();
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [page, setPage] = useState(0);

  if (query.isPending) {
    return (
      <div className="mixer-view" aria-busy="true" aria-label="Loading the mixer">
        <h1 className="sr-only">Mixer</h1>
        <Skeleton className="h-touch-primary w-full" />
      </div>
    );
  }

  if (query.isError) {
    return (
      <div className="mixer-view">
        <h1 className="view-title">Mixer</h1>
        <ErrorState
          title="Could not load the mixer"
          detail="The controller did not answer. Nothing has been changed."
          status={statusLine(query.error)}
          onRetry={() => void query.refetch()}
        />
      </div>
    );
  }

  const data = query.data;
  if (!data || data.device_id === null) {
    return (
      <div className="mixer-view">
        <h1 className="view-title">Mixer</h1>
        <EmptyState icon={SlidersHorizontal} title="No mixer configured" detail="An admin configures the mixer in Mixer configuration." />
      </div>
    );
  }

  if (lawQuery.isPending) {
    return (
      <div className="mixer-view" aria-busy="true" aria-label="Loading the mixer">
        <h1 className="sr-only">Mixer</h1>
        <Skeleton className="h-touch-primary w-full" />
      </div>
    );
  }

  if (lawQuery.isError) {
    return (
      <div className="mixer-view">
        <h1 className="view-title">Mixer</h1>
        <ErrorState
          title="Could not load the fader law"
          detail="The controller did not answer. Nothing has been changed."
          status={statusLine(lawQuery.error)}
          onRetry={() => void lawQuery.refetch()}
        />
      </div>
    );
  }

  const law = lawQuery.data ?? [];
  const { connected } = data;
  // The live store wins once it holds a value, exactly as a channel's own
  // `db`/`muted`/`origin` fall back to `GET /mixer/state` until a frame
  // supersedes them (`./api.ts`'s own module doc) — merged here, once, so
  // every child below keeps reading `capabilities.metering`/`metering_reason`
  // exactly as before and reacts to it without knowing where it came from.
  const capabilities: MixerCapabilities = metering
    ? {
        ...data.capabilities,
        metering: metering.available,
        metering_reason: metering.available ? null : asMeteringReason(metering.reason),
      }
    : data.capabilities;
  const pages = paginate(data.inputs, DEFAULT_PAGE_SIZE);
  const currentPage = Math.min(page, Math.max(0, pages.length - 1));
  const currentInputs = pages[currentPage] ?? [];
  const outputsPanelId = "mixer-outputs-panel";

  return (
    <div className="mixer-view">
      <h1 className="sr-only">Mixer</h1>
      <DeskSceneBand
        scenes={data.desk_scenes}
        lastRecalledScene={data.last_recalled_scene}
        capabilities={capabilities}
        connected={connected}
      />
      {!capabilities.metering ? (
        // Absent, not empty: an absent meter bar reads as "not offered", an
        // empty one reads as silence — this line is what tells the truth (§21.13).
        <p className="mixer-metering-notice" role="status">
          {meteringNotice(capabilities.metering_reason)}
        </p>
      ) : null}
      {!capabilities.pan && data.inputs.some((channel) => channel.show_pan) ? (
        // §5.5 "Capability degradation": configured pan stays visible and
        // disabled, with the reason said once rather than on every strip.
        <p className="mixer-metering-notice" role="status">
          Pan unavailable — {PAN_UNAVAILABLE.charAt(0).toLowerCase() + PAN_UNAVAILABLE.slice(1)}.
        </p>
      ) : null}
      <div className="mixer-body tablet:flex-row">
        <div className="mixer-inputs-panel">
          <div className="mixer-inputs-header">
            <h2 className="lighting-section-title">Inputs</h2>
            {pages.length > 0 ? (
              <div className="mixer-page-chips" role="tablist" aria-label="Input pages">
                {pages.map((_, index) => (
                  <button
                    key={index}
                    type="button"
                    role="tab"
                    aria-selected={index === currentPage}
                    className="mixer-page-chip"
                    onClick={() => setPage(index)}
                  >
                    {pageChipLabel(index, DEFAULT_PAGE_SIZE, data.inputs.length)}
                  </button>
                ))}
              </div>
            ) : null}
            <button
              type="button"
              className="mixer-outputs-toggle"
              aria-expanded={drawerOpen}
              aria-controls={outputsPanelId}
              onClick={() => setDrawerOpen((open) => !open)}
            >
              {drawerOpen ? <PanelRightClose aria-hidden="true" className="size-4" /> : <PanelRightOpen aria-hidden="true" className="size-4" />}
              Outputs
            </button>
          </div>
          {currentInputs.length === 0 ? (
            <p className="mixer-no-inputs">No input channels configured.</p>
          ) : (
            <div className="mixer-input-strips">
              {currentInputs.map((channel) => (
                <ChannelStrip
                  key={channel.channel_id}
                  channel={channel}
                  target={channel.channel_id}
                  law={law}
                  capabilities={capabilities}
                  connected={connected}
                  testId={`mixer-input-${channel.channel_id}`}
                />
              ))}
            </div>
          )}
        </div>
        <OutputsPanel
          id={outputsPanelId}
          open={drawerOpen}
          outputs={data.outputs}
          law={law}
          capabilities={capabilities}
          connected={connected}
        />
        <MainPanel main={data.main} law={law} capabilities={capabilities} connected={connected} />
      </div>
    </div>
  );
}
