/*
 * Admin → Hirer Access (§21.20, §15.12, §6.7, docs/admin-screens.html
 * "Hirer Access"): pages control *what* a hirer reaches, ceilings control
 * *how far* (§15.4, B61). The PIN and the kill switch (`PinAccessCard`) act
 * the instant they are pressed — everything below is one `PUT /hirer/config`
 * saved together, because the contract carries pages, ceilings and the three
 * switches in a single body.
 *
 * Every page's items are fetched here — not just the assigned ones —
 * because ticking an unassigned page must show its ceilings without a
 * save-and-reopen round trip.
 */
import { useState } from "react";
import { useQueries, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import { ConflictDialog } from "@/admin/devices/ConflictDialog";
import { diffRecord } from "@/admin/lighting/diff";
import { useFaderLaw, useMixerState } from "@/admin/mixer/api";
import { HirerCeilingControl } from "@/admin/mixer/HirerCeilingControl";
import { pagesKeys, usePages } from "@/admin/pages/api";
import type { PageDetail, PageListItem } from "@/admin/pages/types";
import { ApiError, api } from "@/api/client";
import { presentError } from "@/api/errors";
import { Banner } from "@/components/ui/Banner";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { ErrorState, Skeleton } from "@/components/ui/EmptyState";
import { Checkbox } from "@/components/ui/Select";
import { HelpButton } from "@/help/HelpButton";
import { saveOnShortcut } from "@/lib/keyboard";

import { hirerKeys, useHirerConfig, useHirerConflicts, useUpdateHirerConfig } from "./api";
import { conflictKey, conflictMessage } from "./conflicts";
import { pageContentsSummary, reachableChannelsAcross, unreachableOutputsOf } from "./model";
import { PinAccessCard } from "./PinAccessCard";
import type { HirerConfig, PutHirerConfigBody } from "./types";

function statusLine(error: unknown): string | undefined {
  return error instanceof ApiError ? `${error.status} ${error.code}` : undefined;
}

export function HirerAccessScreen() {
  const client = useQueryClient();
  const configQuery = useHirerConfig();
  const pagesQuery = usePages();
  const conflictsQuery = useHirerConflicts();
  const mixerState = useMixerState();
  const faderLaw = useFaderLaw(mixerState.data?.device_id ?? null);
  const update = useUpdateHirerConfig();

  // The default page is generated, never assigned (§21.9, §21.20) — it is never offered here.
  const orderedPages = [...(pagesQuery.data ?? [])]
    .filter((page) => !page.is_default)
    .sort((a, b) => a.sort_order - b.sort_order);

  const pageDetailQueries = useQueries({
    queries: orderedPages.map((page) => ({
      queryKey: pagesKeys.detail(page.id),
      queryFn: () => api<PageDetail>(`/pages/${page.id}`),
    })),
  });

  const [syncedVersion, setSyncedVersion] = useState<string | undefined>(undefined);
  const [selectedPageIds, setSelectedPageIds] = useState<Set<number>>(new Set());
  const [ceilingValues, setCeilingValues] = useState<Map<number, number | null>>(new Map());
  const [lightingEnabled, setLightingEnabled] = useState(true);
  const [individualFixtures, setIndividualFixtures] = useState(true);
  const [colourEnabled, setColourEnabled] = useState(true);
  const [saveError, setSaveError] = useState<string | undefined>();
  const [conflict, setConflict] = useState<{ current: HirerConfig } | null>(null);

  const config = configQuery.data;

  // Re-derive the local form only when the config's own `updated_at` changes,
  // the same pattern `PageEditor.tsx` uses, so ticking a page mid-edit is
  // never wiped out by an unrelated refetch.
  if (config && config.updated_at !== syncedVersion) {
    setSyncedVersion(config.updated_at);
    setSelectedPageIds(new Set(config.pages));
    setCeilingValues(new Map(config.ceilings.map((ceiling) => [ceiling.channel_id, ceiling.hirer_max_db])));
    setLightingEnabled(config.lighting_enabled);
    setIndividualFixtures(config.individual_fixtures);
    setColourEnabled(config.colour_enabled);
  }

  const pagesLoading = pagesQuery.isPending || pageDetailQueries.some((query) => query.isPending);
  const pagesFailed = pagesQuery.isError || pageDetailQueries.some((query) => query.isError);

  const pagesWithDetail = orderedPages.map((page, index) => ({ page, detail: pageDetailQueries[index]?.data }));
  const selectedPages: { page: PageListItem; detail: PageDetail }[] = pagesWithDetail.filter(
    (row): row is { page: PageListItem; detail: PageDetail } => selectedPageIds.has(row.page.id) && row.detail !== undefined,
  );

  const reachable = reachableChannelsAcross(selectedPages.map((row) => row.detail));
  const unreachableOutputs = unreachableOutputsOf(
    selectedPages.map(({ page, detail }) => ({ id: page.id, name: page.name, detail })),
  );

  function togglePage(pageId: number, checked: boolean): void {
    setSelectedPageIds((prev) => {
      const next = new Set(prev);
      if (checked) next.add(pageId);
      else next.delete(pageId);
      return next;
    });
  }

  function setCeiling(channelId: number, value: number | null): void {
    setCeilingValues((prev) => {
      const next = new Map(prev);
      next.set(channelId, value);
      return next;
    });
  }

  function buildBody(): PutHirerConfigBody {
    return {
      pages: orderedPages.filter((page) => selectedPageIds.has(page.id)).map((page) => page.id),
      ceilings: reachable.map((channel) => ({ channel_id: channel.channel_id, hirer_max_db: ceilingValues.get(channel.channel_id) ?? null })),
      lighting_enabled: lightingEnabled,
      individual_fixtures: individualFixtures,
      colour_enabled: colourEnabled,
    };
  }

  function save(overrideVersion?: string): void {
    if (!config) return;
    setSaveError(undefined);
    update.mutate(
      { version: overrideVersion ?? config.updated_at, body: buildBody() },
      {
        onSuccess: () => {
          setConflict(null);
          toast.success("Hirer configuration saved");
        },
        onError: (error) => {
          if (error instanceof ApiError) {
            if (error.code === "conflict") {
              const current = error.detail["current"] as HirerConfig | undefined;
              if (current) {
                setConflict({ current });
                return;
              }
            }
            if (error.code === "validation_failed") {
              setSaveError(error.message);
              return;
            }
          }
          presentError(error);
        },
      },
    );
  }

  const dirty =
    config !== undefined &&
    (JSON.stringify([...selectedPageIds].sort((a, b) => a - b)) !== JSON.stringify([...config.pages].sort((a, b) => a - b)) ||
      JSON.stringify(buildBody().ceilings.slice().sort((a, b) => a.channel_id - b.channel_id)) !==
        JSON.stringify(
          config.ceilings
            .map((ceiling) => ({ channel_id: ceiling.channel_id, hirer_max_db: ceiling.hirer_max_db }))
            .sort((a, b) => a.channel_id - b.channel_id),
        ) ||
      lightingEnabled !== config.lighting_enabled ||
      individualFixtures !== config.individual_fixtures ||
      colourEnabled !== config.colour_enabled);

  return (
    <div className="view hirer-access-view" onKeyDown={saveOnShortcut(() => save())}>
      <header className="view-head">
        <div>
          <h1 className="view-title">Hirer Access</h1>
          <p className="view-lede">Pages control what. Ceilings control how far.</p>
        </div>
      </header>

      <PinAccessCard />

      {configQuery.isPending ? (
        <div aria-busy="true" aria-label="Loading hirer configuration">
          <Skeleton className="h-touch w-full" />
          <Skeleton className="h-touch w-full" />
        </div>
      ) : configQuery.isError || !config ? (
        <ErrorState
          title="Could not load the hirer configuration"
          status={statusLine(configQuery.error)}
          onRetry={() => void configQuery.refetch()}
        />
      ) : (
        <>
          <Card className="device-card" title="Pages — what can this hirer reach?" titleLevel="h2">
            {pagesLoading ? (
              <div aria-busy="true" aria-label="Loading pages">
                <Skeleton className="h-touch w-full" />
              </div>
            ) : pagesFailed ? (
              <ErrorState
                title="Could not load pages"
                onRetry={() => {
                  void pagesQuery.refetch();
                  pageDetailQueries.forEach((query) => void query.refetch());
                }}
              />
            ) : orderedPages.length === 0 ? (
              <p className="text-fg-muted text-sm">
                No pages yet — create one on Admin → Pages before assigning it here.
              </p>
            ) : (
              <div className="table-scroll">
                <table className="data-table">
                  <caption className="sr-only">Pages this hirer can reach</caption>
                  <thead>
                    <tr>
                      <th scope="col">Assigned</th>
                      <th scope="col">Page</th>
                      <th scope="col">Contains</th>
                    </tr>
                  </thead>
                  <tbody>
                    {pagesWithDetail.map(({ page, detail }) => (
                      <tr key={page.id}>
                        <td>
                          <Checkbox
                            id={`hirer-page-${page.id}`}
                            label={`Assign "${page.name}"`}
                            checked={selectedPageIds.has(page.id)}
                            onChange={(event) => togglePage(page.id, event.currentTarget.checked)}
                          />
                        </td>
                        <td>{page.name}</td>
                        <td className="text-fg-muted text-sm">{detail ? pageContentsSummary(detail) : "…"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            <p className="field-help">
              A hirer sees only the pages ticked here, and no other views. What they can reach is a layout you can
              open and look at — not the product of several visibility flags across several tables.
            </p>
          </Card>

          {unreachableOutputs.length > 0 ? (
            <div className="flex flex-col gap-2">
              {unreachableOutputs.map((output) => (
                <Banner key={`${output.page_id}:${output.channel_id}`} tone="warning">
                  &ldquo;{output.channel_name}&rdquo; is on &ldquo;{output.page_name}&rdquo; but cannot be reached by a
                  hirer — only Main and input channels can be.
                </Banner>
              ))}
            </div>
          ) : null}

          <Card className="device-card" title="Ceilings — how far can each fader travel?" titleLevel="h2">
            {reachable.length === 0 ? (
              <p className="text-fg-muted text-sm">
                No channels reachable yet — assign a page with an input channel, or Main, to set a ceiling.
              </p>
            ) : (
              <div className="flex flex-col gap-4">
                {reachable.map((channel) => (
                  <div key={channel.channel_id} className="flex flex-col gap-2">
                    <span className="field-label">{channel.name}</span>
                    <HirerCeilingControl
                      id={`hirer-ceiling-${channel.channel_id}`}
                      value={ceilingValues.get(channel.channel_id) ?? null}
                      onChange={(next) => setCeiling(channel.channel_id, next)}
                      law={faderLaw.data?.fader_law ?? []}
                    />
                  </div>
                ))}
              </div>
            )}
            <p className="field-help">
              Only channels reachable from an assigned page are listed. A ceiling applies whenever the session is a
              hirer, whichever page the channel was reached from.
            </p>
          </Card>

          <Card
            className="device-card"
            title={
              <span className="flex items-center gap-2">
                Lighting control
                <HelpButton id="hirer.lighting-control" label="Lighting control" />
              </span>
            }
            titleLevel="h2"
          >
            <Checkbox
              id="hirer-lighting-enabled"
              label="Allow lighting control"
              checked={lightingEnabled}
              onChange={(event) => setLightingEnabled(event.currentTarget.checked)}
            />
            <p className="field-help">Off, no lighting item on the assigned pages is reachable at all.</p>
            <Checkbox
              id="hirer-individual-fixtures"
              label="Individual fixture control"
              checked={individualFixtures}
              onChange={(event) => setIndividualFixtures(event.currentTarget.checked)}
            />
            <p className="field-help">Off, a tray's members are shown but not writable — only the group master moves.</p>
            <Checkbox
              id="hirer-colour-enabled"
              label="Colour control"
              checked={colourEnabled}
              onChange={(event) => setColourEnabled(event.currentTarget.checked)}
            />
            <p className="field-help">Off, colour writes are refused even on a reachable fixture.</p>
          </Card>

          {conflictsQuery.data && conflictsQuery.data.conflicts.length > 0 ? (
            <Card className="device-card" title="Ceiling conflicts" titleLevel="h2">
              <div className="flex flex-col gap-2">
                {conflictsQuery.data.conflicts.map((row) => (
                  <Banner key={conflictKey(row)} tone="warning">
                    {conflictMessage(row)}
                  </Banner>
                ))}
              </div>
              <p className="field-help">Worth catching before a hire rather than during one.</p>
            </Card>
          ) : null}

          <Banner tone="info">
            A rule bypasses this permission model — it can set a group or fire a scene with no ceiling and no channel
            filter (§15.4). A rule a hirer can fire is still a button on a page you can open and review, but check
            what that button's rule actually does before assigning the page.
          </Banner>

          {saveError ? (
            <p className="field-note" role="alert">
              {saveError}
            </p>
          ) : null}

          <div className="flex justify-end">
            <Button variant="primary" helpId="hirer.save" loading={update.isPending} disabled={!dirty} onClick={() => save()}>
              Save changes
            </Button>
          </div>

          <ConflictDialog
            open={conflict !== null}
            onOpenChange={(next) => {
              if (!next) setConflict(null);
            }}
            deviceName="Hirer access"
            rows={conflict ? diffRecord(conflict.current as unknown as Record<string, unknown>, buildBody() as unknown as Record<string, unknown>) : []}
            onReload={() => {
              if (!conflict) return;
              client.setQueryData(hirerKeys.config, conflict.current);
              setConflict(null);
            }}
            onOverwrite={() => conflict && save(conflict.current.updated_at)}
          />
        </>
      )}
    </div>
  );
}
