/*
 * Hirer shell (spec §21.6, §21.15, §15.12): the Pages surface over exactly
 * the pages assigned to this hirer, and nothing else — no Mixer, no
 * Lighting, no stage plan, no configuration surface, never /admin (§6.13).
 * Tabs appear only when more than one page is assigned; with one page the
 * surface renders directly with no tab strip above it (§21.6's wireframe).
 *
 * `PageSurface` is the same component an operator's Pages tab mounts;
 * this shell supplies its `hirer` prop for §24.6's overrides and
 * fetches through the same `usePages`/`usePage` an operator uses — the
 * contract already narrows what a hirer's `GET /pages`/`GET /pages/{id}`
 * return, so no separate hirer-only query exists.
 */
import { Blocks } from "lucide-react";
import { NavLink, Navigate, Route, Routes } from "react-router-dom";

import { ApiError } from "@/api/client";
import { EmptyState, ErrorState, Skeleton } from "@/components/ui/EmptyState";
import { PageSurface } from "@/pagesurface/PageSurface";
import { usePage, usePages } from "@/pagesurface/api";
import type { PageSummary } from "@/pagesurface/types";
import { InstallPrompt } from "@/pwa/InstallPromptCard";

import { HirerDeviceBanner } from "./HirerDeviceBanner";
import { NavLabel } from "./NavLabel";
import { Shell } from "./Shell";

function statusLine(error: unknown): string | undefined {
  return error instanceof ApiError ? `${error.status} ${error.code}` : undefined;
}

function sortedPages(pages: readonly PageSummary[]): readonly PageSummary[] {
  return [...pages].sort((a, b) => a.sort_order - b.sort_order);
}

/** §21.15's plain failure message, not the operator's own technical detail. */
function HirerPage({ pageId, pageName }: { pageId: number; pageName: string }) {
  const query = usePage(pageId);

  const body = (() => {
    if (query.isPending) {
      return (
        <div className="page-surface-flow" aria-busy="true" aria-label={`Loading ${pageName}`}>
          <Skeleton className="h-touch-primary w-full" />
        </div>
      );
    }
    if (query.isError) {
      return (
        <ErrorState
          title="Something went wrong"
          detail="Please speak to venue staff."
          status={statusLine(query.error)}
          onRetry={() => void query.refetch()}
        />
      );
    }
    return query.data ? <PageSurface page={query.data} hirer /> : null;
  })();

  // The hirer surface shows no title of its own; the heading names the page
  // for assistive technology and is where focus lands after choosing a page
  // tab (`useRouteFocus`, §24.7).
  return (
    <>
      <h1 className="sr-only">{pageName}</h1>
      {body}
    </>
  );
}

export function HirerShell() {
  const pagesQuery = usePages();
  const pages = sortedPages(pagesQuery.data?.pages ?? []);
  const firstPage = pages[0];

  const content = (() => {
    if (pagesQuery.isPending) {
      return (
        <div className="page-surface-flow" aria-busy="true" aria-label="Loading your pages">
          <Skeleton className="h-touch-primary w-full" />
        </div>
      );
    }
    if (pagesQuery.isError) {
      return (
        <ErrorState
          title="Something went wrong"
          detail="Please speak to venue staff."
          status={statusLine(pagesQuery.error)}
          onRetry={() => void pagesQuery.refetch()}
        />
      );
    }
    if (pages.length === 0 || !firstPage) {
      return <EmptyState icon={Blocks} title="No controls available" detail="Nothing has been assigned to you yet." />;
    }
    return (
      <Routes>
        <Route index element={<Navigate to={String(firstPage.id)} replace />} />
        {pages.map((page) => (
          <Route key={page.id} path={String(page.id)} element={<HirerPage pageId={page.id} pageName={page.name} />} />
        ))}
        {/* An unassigned page id, or anything else under /hire, lands back on the first assigned page. */}
        <Route path="*" element={<Navigate to={String(firstPage.id)} replace />} />
      </Routes>
    );
  })();

  return (
    <Shell
      tier="hirer"
      manifest="hirer"
      header={
        pages.length > 1 ? (
          <nav className="tab-strip" aria-label="Pages">
            {pages.map((page) => (
              <NavLink key={page.id} to={`/hire/${page.id}`} className="tab h-touch-hirer min-w-touch-hirer justify-center">
                {({ isActive }) => <NavLabel active={isActive}>{page.name}</NavLabel>}
              </NavLink>
            ))}
          </nav>
        ) : undefined
      }
    >
      <InstallPrompt />
      {pages.length > 0 ? <HirerDeviceBanner pages={pages} /> : null}
      {content}
    </Shell>
  );
}
