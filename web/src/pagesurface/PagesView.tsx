/*
 * The operator Pages route (spec §21.9 — its wireframe is the specification
 * for this view): page tabs from `GET /pages`, the selected page's resolved
 * items from `GET /pages/{id}`, and the display-scale control. The tabs and
 * fetching here are operator-shell wiring; `PageSurface` is the reusable
 * piece the hirer shell (`@/shells/HirerShell`) also mounts directly.
 */
import { Blocks } from "lucide-react";
import { useState } from "react";

import { ApiError } from "@/api/client";
import { EmptyState, ErrorState, Skeleton } from "@/components/ui/EmptyState";

import { DisplayScaleControl } from "./DisplayScaleControl";
import { PageSurface } from "./PageSurface";
import { usePage, usePages } from "./api";
import type { PageSummary } from "./types";

function statusLine(error: unknown): string | undefined {
  return error instanceof ApiError ? `${error.status} ${error.code}` : undefined;
}

function sortedPages(pages: readonly PageSummary[]): readonly PageSummary[] {
  return [...pages].sort((a, b) => a.sort_order - b.sort_order);
}

function PageDetailPane({ pageId, pageName }: { pageId: number; pageName: string }) {
  const query = usePage(pageId);

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
        title={`Could not load ${pageName}`}
        detail="The controller did not answer. Nothing has been changed."
        status={statusLine(query.error)}
        onRetry={() => void query.refetch()}
      />
    );
  }

  return query.data ? <PageSurface page={query.data} /> : null;
}

export function PagesView() {
  const pagesQuery = usePages();
  const [selectedId, setSelectedId] = useState<number | null>(null);

  if (pagesQuery.isPending) {
    return (
      <div className="page-surface-view" aria-busy="true" aria-label="Loading pages">
        <h1 className="sr-only">Pages</h1>
        <Skeleton className="h-touch w-full" />
      </div>
    );
  }

  if (pagesQuery.isError) {
    return (
      <div className="page-surface-view">
        <h1 className="view-title">Pages</h1>
        <ErrorState
          title="Could not load pages"
          detail="The controller did not answer. Nothing has been changed."
          status={statusLine(pagesQuery.error)}
          onRetry={() => void pagesQuery.refetch()}
        />
      </div>
    );
  }

  const pages = sortedPages(pagesQuery.data?.pages ?? []);
  if (pages.length === 0) {
    return (
      <div className="page-surface-view">
        <h1 className="view-title">Pages</h1>
        <EmptyState
          icon={Blocks}
          title="No pages yet"
          detail="An admin builds pages from the channels and buttons the room needs to hand."
        />
      </div>
    );
  }

  const firstPage = pages[0] as PageSummary;
  const active = pages.find((page) => page.id === selectedId) ?? firstPage;

  return (
    <div className="page-surface-view">
      <h1 className="sr-only">Pages</h1>
      <div className="page-tabs-row">
        <div className="page-tabs tab-strip" role="tablist" aria-label="Pages">
          {pages.map((page) => (
            <button
              key={page.id}
              type="button"
              role="tab"
              aria-selected={page.id === active.id}
              className="tab"
              onClick={() => setSelectedId(page.id)}
            >
              {page.name}
            </button>
          ))}
        </div>
        <DisplayScaleControl />
      </div>
      <PageDetailPane key={active.id} pageId={active.id} pageName={active.name} />
    </div>
  );
}
