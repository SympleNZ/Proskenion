/*
 * The operator Mixer view (spec §21.13; `docs/operator-views.html`'s Mixer
 * tab is the drawn reference). The desk row, left to right: the input
 * strips, a hairline, the outputs drawer while it is open, and Main — pinned
 * immediately after the inputs, as the mock draws it, so it is never
 * stranded at the far edge of a wide screen. Everything is built on the
 * shared `FaderStrip` card via `ChannelStrip` and the mixer service's
 * contract (`docs/plans/phase-4-contracts.md`).
 *
 * How many inputs show is measured, not fixed (§21.13 "calculated from
 * available width"): `inputLayout` answers from the desk row's own width,
 * so a 4K monitor shows every input at once and page chips appear only where
 * they do not all fit (an iPad in portrait). The drawer narrows the inputs by
 * the outputs' own width; where that leaves no room for one input, the
 * inputs give way to the outputs (§21.13's narrow "outputs page").
 *
 * The row fills the height the view has left, so the faders are as tall as
 * the screen allows (capped, `--fader-strip-max-height`) and never shorter
 * than the mock's card.
 */
import { PanelRightClose, PanelRightOpen, SlidersHorizontal } from "lucide-react";
import { useState, type CSSProperties } from "react";

import { ApiError } from "@/api/client";
import { ScrollRow } from "@/components/scrollrow/ScrollRow";
import { EmptyState, ErrorState, Skeleton } from "@/components/ui/EmptyState";
import type { FaderLaw } from "@/lib/faderLaw";
import { useElementSize } from "@/lib/useElementSize";
import { useMixerMetering } from "@/live/store";

import { useFaderLaw, useMixerState } from "./api";
import { ChannelStrip, PAN_UNAVAILABLE } from "./ChannelStrip";
import { DeskSceneBand } from "./DeskSceneBand";
import { inputLayout, pageChipLabel, paginate } from "./pagination";
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
  /** How many strips wide the open drawer is (`inputLayout`); the rest scroll inside it. */
  shown: number;
  outputs: readonly MixerOutputChannel[];
  law: FaderLaw;
  capabilities: MixerCapabilities;
  connected: boolean;
}

/**
 * The drawer (§21.13): always mounted, so opening it is a width change on a
 * panel that is already there. Its open width is arithmetic — the strips
 * `inputLayout` gave it × (strip + gap) — carried to CSS as
 * `--mixer-output-count`; any outputs beyond that scroll inside it.
 */
function OutputsPanel({ id, open, shown, outputs, law, capabilities, connected }: OutputsPanelProps) {
  return (
    <div
      id={id}
      className="mixer-column mixer-outputs-panel"
      data-open={open || undefined}
      aria-hidden={!open}
      inert={!open}
      style={{ "--mixer-output-count": shown } as CSSProperties}
    >
      <div className="mixer-column-header">
        <h2 className="lighting-section-title">Outputs</h2>
      </div>
      <div className="mixer-strips">
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

function MainStrip({ main, law, capabilities, connected }: { main: NonNullable<MixerStateResponse["main"]>; law: FaderLaw; capabilities: MixerCapabilities; connected: boolean }) {
  return (
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
  const [deskRef, deskSize] = useElementSize<HTMLDivElement>();

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
  const layout = inputLayout(deskSize.width, data.inputs.length, drawerOpen, data.outputs.length);
  const pageSize = layout.pageSize;
  const pages = paginate(data.inputs, pageSize);
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
        <p className="mixer-metering-notice">
          {meteringNotice(capabilities.metering_reason)}
        </p>
      ) : null}
      {!capabilities.pan && data.inputs.some((channel) => channel.show_pan) ? (
        // §5.5 "Capability degradation": configured pan stays visible and
        // disabled, with the reason said once rather than on every strip.
        <p className="mixer-metering-notice">
          Pan unavailable — {PAN_UNAVAILABLE.charAt(0).toLowerCase() + PAN_UNAVAILABLE.slice(1)}.
        </p>
      ) : null}
      <ScrollRow scrollerRef={deskRef} rowClassName="mixer-desk-row" className="mixer-desk">
        <div className="mixer-column mixer-inputs-panel" hidden={layout.inputsHidden}>
          <div className="mixer-column-header">
            <h2 className="lighting-section-title">Inputs</h2>
            {pages.length > 1 ? (
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
                    {pageChipLabel(index, pageSize, data.inputs.length)}
                  </button>
                ))}
              </div>
            ) : null}
          </div>
          {currentInputs.length === 0 ? (
            <p className="mixer-no-inputs">No input channels configured.</p>
          ) : (
            <div className="mixer-strips mixer-input-strips">
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
        <div className="mixer-divider" aria-hidden="true" hidden={layout.inputsHidden} />
        <OutputsPanel
          id={outputsPanelId}
          open={drawerOpen}
          shown={layout.outputsShown}
          outputs={data.outputs}
          law={law}
          capabilities={capabilities}
          connected={connected}
        />
        <div className="mixer-column mixer-main-panel">
          <div className="mixer-column-header">
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
          {data.main ? (
            <div className="mixer-strips">
              <MainStrip main={data.main} law={law} capabilities={capabilities} connected={connected} />
            </div>
          ) : null}
        </div>
      </ScrollRow>
    </div>
  );
}
