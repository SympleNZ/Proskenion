/*
 * Admin → Pages (§21.9, §15.12, docs/admin-screens.html "Pages" — first in
 * the Control nav group). The list: create, rename, reorder and delete, with
 * the generated default page shown read-only (§21.9 "generated, never
 * required"; the contract refuses both its `PUT` and its `DELETE` with
 * `detail.reason = "default_page""). Renaming happens inside the editor,
 * alongside every other change to the page, because the contract has no
 * separate rename endpoint — one `PUT` carries the whole page.
 */
import { useState } from "react";
import { ArrowDown, ArrowUp, LayoutGrid } from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";

import { ApiError, api } from "@/api/client";
import { presentError } from "@/api/errors";
import { Banner } from "@/components/ui/Banner";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { ConfirmDialog } from "@/components/ui/Sheet";
import { EmptyState, ErrorState, Skeleton } from "@/components/ui/EmptyState";
import { Input } from "@/components/ui/Input";
import { FieldLabel } from "@/help/HelpButton";

import { editorItemsToPutItems, pageDetailToEditorItems } from "./editorModel";
import { PageEditor } from "./PageEditor";
import { pagesKeys, useCreatePage, useDeletePage, usePages, VERSION_HEADER } from "./api";
import type { PageDetail, PageListItem, PutPageBody } from "./types";

function statusLine(error: unknown): string | undefined {
  return error instanceof ApiError ? `${error.status} ${error.code}` : undefined;
}

export function PagesScreen() {
  const pages = usePages();
  const create = useCreatePage();
  const remove = useDeletePage();
  const client = useQueryClient();

  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");
  const [createError, setCreateError] = useState<string | undefined>();
  const [deleteTarget, setDeleteTarget] = useState<PageListItem | null>(null);
  const [deleteError, setDeleteError] = useState<string | undefined>();
  const [reordering, setReordering] = useState<number | null>(null);

  const rows = [...(pages.data ?? [])].sort((a, b) => a.sort_order - b.sort_order);

  function submitCreate(): void {
    const trimmed = newName.trim();
    if (!trimmed) {
      setCreateError("Give the page a name");
      return;
    }
    setCreateError(undefined);
    create.mutate(
      { name: trimmed },
      {
        onSuccess: (page) => {
          setCreating(false);
          setNewName("");
          setSelectedId(page.id);
        },
        onError: (error) => presentError(error),
      },
    );
  }

  function requestDelete(page: PageListItem): void {
    setDeleteError(undefined);
    remove.mutate(page.id, {
      onSuccess: () => {
        setDeleteTarget(null);
        if (selectedId === page.id) setSelectedId(null);
      },
      onError: (error) => {
        if (error instanceof ApiError) {
          setDeleteError(error.message);
          return;
        }
        presentError(error);
      },
    });
  }

  /**
   * Reordering swaps `sort_order` between two adjacent pages (mirrors
   * `SceneEditor.tsx`'s `moveAction`). The contract's `PUT` always carries the
   * whole page, so each side's current items are read first and sent back
   * unchanged — only `sort_order` moves.
   */
  async function movePage(page: PageListItem, direction: -1 | 1): Promise<void> {
    const index = rows.findIndex((row) => row.id === page.id);
    const neighbour = rows[index + direction];
    if (!neighbour) return;
    setReordering(page.id);
    try {
      const [a, b] = await Promise.all([
        client.fetchQuery({ queryKey: pagesKeys.detail(page.id), queryFn: () => api<PageDetail>(`/pages/${page.id}`) }),
        client.fetchQuery({ queryKey: pagesKeys.detail(neighbour.id), queryFn: () => api<PageDetail>(`/pages/${neighbour.id}`) }),
      ]);
      const bodyA: PutPageBody = { name: a.name, sort_order: b.sort_order, items: editorItemsToPutItems(pageDetailToEditorItems(a)) };
      const bodyB: PutPageBody = { name: b.name, sort_order: a.sort_order, items: editorItemsToPutItems(pageDetailToEditorItems(b)) };
      await api<PageDetail>(`/pages/${a.id}`, { method: "PUT", body: bodyA, headers: { [VERSION_HEADER]: a.updated_at } });
      await api<PageDetail>(`/pages/${b.id}`, { method: "PUT", body: bodyB, headers: { [VERSION_HEADER]: b.updated_at } });
      await client.invalidateQueries({ queryKey: pagesKeys.list });
      client.removeQueries({ queryKey: pagesKeys.detail(a.id) });
      client.removeQueries({ queryKey: pagesKeys.detail(b.id) });
    } catch (error) {
      presentError(error);
    } finally {
      setReordering(null);
    }
  }

  return (
    <div className="view pages-view">
      <header className="view-head">
        <div>
          <h1 className="view-title">Pages</h1>
          <p className="view-lede">
            The everyday surface (§21.9). Channel strips, group trays and button panels in one layout, grouped by
            activity — Mixer and Lighting remain the advanced view.
          </p>
        </div>
        <Button variant="primary" helpId="pages.add" onClick={() => { setCreating(true); setCreateError(undefined); setNewName(""); }}>
          + Add page
        </Button>
      </header>

      {pages.isPending ? (
        <div aria-busy="true" aria-label="Loading pages">
          <Skeleton className="h-touch w-full" />
          <Skeleton className="h-touch w-full" />
        </div>
      ) : pages.isError ? (
        <ErrorState title="Could not load pages" status={statusLine(pages.error)} onRetry={() => void pages.refetch()} />
      ) : rows.length === 0 ? (
        <EmptyState icon={LayoutGrid} title="No pages yet" detail="Create the first page to give the room something to run from." />
      ) : (
        <Card className="device-card" compact>
          <div className="table-scroll">
            <table className="data-table">
              <caption className="sr-only">The pages this controller knows about</caption>
              <thead>
                <tr>
                  <th scope="col">
                    <span className="sr-only">Order</span>
                  </th>
                  <th scope="col">Name</th>
                  <th scope="col">Hirer</th>
                  <th scope="col">
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {rows.map((page, index) => (
                  <tr key={page.id} aria-selected={selectedId === page.id || undefined}>
                    <td className="device-actions">
                      <Button
                        variant="ghost"
                        size="icon"
                        aria-label={`Move "${page.name}" earlier`}
                        disabled={page.is_default || index === 0 || reordering !== null}
                        onClick={() => void movePage(page, -1)}
                      >
                        <ArrowUp aria-hidden="true" className="size-4" />
                      </Button>
                      <Button
                        variant="ghost"
                        size="icon"
                        aria-label={`Move "${page.name}" later`}
                        disabled={page.is_default || index === rows.length - 1 || reordering !== null}
                        onClick={() => void movePage(page, 1)}
                      >
                        <ArrowDown aria-hidden="true" className="size-4" />
                      </Button>
                    </td>
                    <td>
                      {page.name}
                      {page.is_default ? <span className="pill">Generated</span> : null}
                    </td>
                    <td>{page.hirer ? <span className="pill" data-tone="success">Assigned</span> : <span className="pill">—</span>}</td>
                    <td className="device-actions">
                      <Button variant="secondary" onClick={() => setSelectedId(page.id)}>
                        {page.is_default ? "View" : "Edit"}
                      </Button>
                      {!page.is_default ? (
                        <Button
                          variant="destructive"
                          helpId="pages.delete"
                          onClick={() => {
                            setDeleteError(undefined);
                            setDeleteTarget(page);
                          }}
                          loading={remove.isPending}
                        >
                          Delete
                        </Button>
                      ) : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}

      {rows.some((page) => page.is_default) ? (
        <Banner tone="info">
          The generated page is read-only: it is rebuilt from every visible mixer channel and lighting group whenever
          configuration changes, and it can never be assigned to a hirer (§21.9).
        </Banner>
      ) : null}

      {selectedId !== null ? <PageEditor pageId={selectedId} onClose={() => setSelectedId(null)} /> : null}

      {creating ? (
        <Card title="Add page" className="device-card">
          <div className="field schema-field">
            <FieldLabel htmlFor="new-page-name" help="pages.new.name">
              Name
            </FieldLabel>
            <Input id="new-page-name" value={newName} onChange={(event) => setNewName(event.currentTarget.value)} autoFocus />
            {createError ? (
              <p className="field-note" role="alert">
                {createError}
              </p>
            ) : null}
          </div>
          <div className="dialog-actions">
            <Button variant="secondary" onClick={() => setCreating(false)}>
              Cancel
            </Button>
            <Button variant="primary" helpId="pages.add" loading={create.isPending} onClick={submitCreate}>
              Add page
            </Button>
          </div>
        </Card>
      ) : null}

      <ConfirmDialog
        open={deleteTarget !== null}
        onOpenChange={(open) => { if (!open) setDeleteTarget(null); }}
        title={`Delete "${deleteTarget?.name}"?`}
        description={deleteError ?? "Removes the page and, if it was assigned, the hirer's access to it. This cannot be undone."}
        confirmLabel="Delete"
        destructive
        onConfirm={() => { if (deleteTarget) requestDelete(deleteTarget); }}
      />
    </div>
  );
}
