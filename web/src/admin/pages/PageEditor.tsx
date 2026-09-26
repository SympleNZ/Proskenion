/*
 * One page's editor (§21.9, §15.12, docs/admin-screens.html "Pages"): the
 * item canvas — channel strips, group masters and button panels in one
 * ordered list — plus per-kind detail fields and the validator. Saving is
 * one `PUT` of the whole page (contract: "It replaces every item and button
 * in one transaction"), so there is no per-item network call here — every
 * change is local until "Save page".
 *
 * The default page is generated and read-only (§21.9): the contract refuses
 * its `PUT` outright, so this screen never offers one for it.
 */
import { useState } from "react";
import { ChevronLeft } from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";

import { ConflictDialog } from "@/admin/devices/ConflictDialog";
import { diffRecord } from "@/admin/lighting/diff";
import { useMixerChannels } from "@/admin/mixer/api";
import { ApiError } from "@/api/client";
import { presentError } from "@/api/errors";
import { Banner } from "@/components/ui/Banner";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { ErrorState, Skeleton } from "@/components/ui/EmptyState";
import { Input } from "@/components/ui/Input";
import { Checkbox, Select } from "@/components/ui/Select";
import { FieldLabel } from "@/help/HelpButton";
import { useLightingChannels, useLightingGroups } from "@/lighting/api";
import { saveOnShortcut } from "@/lib/keyboard";

import { ButtonEditorSheet } from "./ButtonEditorSheet";
import { groupColourVar } from "./colours";
import { pagesKeys, usePageDetail, useUpdatePage, useValidatePage } from "./api";
import {
  clampPanelWidth,
  duplicateMemberItemKeys,
  editorItemsToPutItems,
  firstIncompleteItem,
  MAX_PANEL_WIDTH,
  MIN_PANEL_WIDTH,
  newChannelItem,
  newGroupMasterItem,
  newPanelItem,
  pageDetailToEditorItems,
  type EditorButton,
  type EditorChannelItem,
  type EditorGroupMasterItem,
  type EditorItem,
  type EditorPanelItem,
} from "./editorModel";
import type { PageDetail, PutPageBody, ValidateFinding } from "./types";

function statusLine(error: unknown): string | undefined {
  return error instanceof ApiError ? `${error.status} ${error.code}` : undefined;
}

function itemSummary(item: EditorItem): string {
  if (item.kind === "channel") {
    const kind = item.source === "mixer" ? "Mixer" : "Lighting";
    return `${kind} · ${item.label || "Choose a channel…"}`;
  }
  if (item.kind === "group_master") {
    return `Group · ${item.groupName || "Choose a group…"} (${item.members.length} member${item.members.length === 1 ? "" : "s"})`;
  }
  return `Panel · ${item.panelTitle || "Untitled"} (${item.buttons.length} button${item.buttons.length === 1 ? "" : "s"})`;
}

export interface PageEditorProps {
  pageId: number;
  onClose: () => void;
}

interface FormState {
  name: string;
  sortOrder: number;
  items: EditorItem[];
}

export function PageEditor({ pageId, onClose }: PageEditorProps) {
  const client = useQueryClient();
  const detail = usePageDetail(pageId);
  const update = useUpdatePage();
  const validate = useValidatePage();

  const [form, setForm] = useState<FormState | null>(null);
  const [syncedVersion, setSyncedVersion] = useState<string | undefined>(undefined);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [nameError, setNameError] = useState<string | undefined>();
  const [saveError, setSaveError] = useState<string | undefined>();
  const [conflict, setConflict] = useState<{ current: PageDetail } | null>(null);
  const [findings, setFindings] = useState<ValidateFinding[] | null>(null);
  const [buttonSheet, setButtonSheet] = useState<{
    panelKey: string;
    button: EditorButton | undefined;
    col: number;
    row: number;
  } | null>(null);

  const page = detail.data;

  // Re-derive the local form only when the page's own `updated_at` changes —
  // not on every refetch — so mid-edit typing is never wiped out. Comparing
  // an identity key during render is React's documented way to adjust state
  // from data that just arrived (mirrors `SceneEditor.tsx`).
  if (page && page.updated_at !== syncedVersion) {
    setSyncedVersion(page.updated_at);
    setForm({ name: page.name, sortOrder: page.sort_order, items: pageDetailToEditorItems(page) });
    setFindings(null);
  }

  if (detail.isPending || !form) {
    return (
      <div aria-busy="true" aria-label="Loading the page">
        <Skeleton className="h-touch w-full" />
        <Skeleton className="h-touch w-full" />
      </div>
    );
  }

  if (detail.isError || !page) {
    return (
      <ErrorState
        title="Could not load this page"
        status={statusLine(detail.error)}
        onRetry={() => void detail.refetch()}
        onBack={onClose}
      />
    );
  }

  const readOnly = page.is_default;
  const dirty =
    !readOnly &&
    (form.name !== page.name ||
      form.sortOrder !== page.sort_order ||
      JSON.stringify(editorItemsToPutItems(form.items)) !== JSON.stringify(editorItemsToPutItems(pageDetailToEditorItems(page))));

  function updateItems(next: EditorItem[]): void {
    setForm((current) => (current ? { ...current, items: next } : current));
  }

  function addItem(item: EditorItem): void {
    setForm((current) => (current ? { ...current, items: [...current.items, item] } : current));
    setSelectedKey(item.key);
  }

  function removeItem(key: string): void {
    if (!form) return;
    updateItems(form.items.filter((item) => item.key !== key));
    if (selectedKey === key) setSelectedKey(null);
  }

  function moveItem(key: string, direction: -1 | 1): void {
    if (!form) return;
    const index = form.items.findIndex((item) => item.key === key);
    const target = index + direction;
    if (index < 0 || target < 0 || target >= form.items.length) return;
    const next = [...form.items];
    const a = next[index];
    const b = next[target];
    if (!a || !b) return;
    next[index] = b;
    next[target] = a;
    updateItems(next);
  }

  function replaceItem(key: string, next: EditorItem): void {
    if (!form) return;
    updateItems(form.items.map((item) => (item.key === key ? next : item)));
  }

  function save(overrideVersion?: string): void {
    if (!form || !page) return;
    const trimmed = form.name.trim();
    if (!trimmed) {
      setNameError("Give the page a name");
      return;
    }
    setNameError(undefined);
    const incomplete = firstIncompleteItem(form.items);
    if (incomplete) {
      setSaveError(incomplete);
      return;
    }
    setSaveError(undefined);
    const body: PutPageBody = { name: trimmed, sort_order: form.sortOrder, items: editorItemsToPutItems(form.items) };
    update.mutate(
      { id: page.id, version: overrideVersion ?? page.updated_at, body },
      {
        onSuccess: () => setConflict(null),
        onError: (error) => {
          if (error instanceof ApiError) {
            if (error.code === "conflict") {
              const current = error.detail["current"] as PageDetail | undefined;
              if (current) {
                setConflict({ current });
                return;
              }
            }
            if (error.code === "validation_failed") {
              const field = error.detail["field"];
              setSaveError(typeof field === "string" ? `${error.message} — button ${field}` : error.message);
              return;
            }
          }
          presentError(error);
        },
      },
    );
  }

  function runValidate(): void {
    if (!page) return;
    validate.mutate(page.id, {
      onSuccess: (result) => setFindings(result.findings),
      onError: (error) => presentError(error),
    });
  }

  function removeDuplicateMembers(): void {
    if (!form) return;
    const keys = new Set(duplicateMemberItemKeys(form.items));
    if (keys.size === 0) return;
    updateItems(form.items.filter((item) => !keys.has(item.key)));
    setFindings((current) => (current ? current.filter((finding) => finding.code !== "duplicate_member") : current));
  }

  const selected = form.items.find((item) => item.key === selectedKey) ?? null;

  return (
    <Card
      className="device-card page-editor"
      title={readOnly ? `${page.name} (generated)` : page.name}
      onKeyDown={readOnly ? undefined : saveOnShortcut(() => save())}
    >
      <div className="view-head">
        <Button variant="ghost" size="icon" aria-label="Back to pages" onClick={onClose}>
          <ChevronLeft aria-hidden="true" className="size-5" />
        </Button>
        {!readOnly ? (
          <div className="flex gap-3">
            <Button variant="secondary" loading={validate.isPending} onClick={runValidate}>
              Validate
            </Button>
            <Button variant="primary" helpId="pages.save" loading={update.isPending} disabled={!dirty} onClick={() => save()}>
              Save page
            </Button>
          </div>
        ) : null}
      </div>

      {readOnly ? (
        <Banner tone="info">
          This page is generated automatically from every visible mixer channel and lighting group, and is
          regenerated whenever that configuration changes. It cannot be edited, and it can never be assigned to a
          hirer (§21.9).
        </Banner>
      ) : (
        <div className="field schema-field">
          <FieldLabel htmlFor="page-editor-name" help="pages.name">
            Name
          </FieldLabel>
          <Input
            id="page-editor-name"
            value={form.name}
            onChange={(event) => setForm((current) => (current ? { ...current, name: event.currentTarget.value } : current))}
          />
          {nameError ? (
            <p className="field-note" role="alert">
              {nameError}
            </p>
          ) : null}
        </div>
      )}

      {saveError ? (
        <p className="field-note" role="alert">
          {saveError}
        </p>
      ) : null}

      {findings && findings.length > 0 ? (
        <div className="flex flex-col gap-2">
          {findings.map((finding, index) => {
            const item = form.items.find((candidate) => candidate.id === finding.item_id);
            return (
              <Banner
                key={`${finding.code}-${finding.item_id}-${index}`}
                tone="warning"
                title={item ? itemSummary(item) : undefined}
                action={
                  finding.code === "duplicate_member" ? (
                    <Button variant="secondary" onClick={removeDuplicateMembers}>
                      Remove duplicates
                    </Button>
                  ) : undefined
                }
              >
                {finding.message}
              </Banner>
            );
          })}
        </div>
      ) : findings && findings.length === 0 ? (
        <Banner tone="success">No problems found.</Banner>
      ) : null}

      <div className="flex flex-col gap-3">
        <h2 className="section-title">Items</h2>
        <div className="page-item-canvas">
          {form.items.length === 0 ? (
            <p className="text-fg-muted text-sm">No items yet — add a channel, group master or panel below.</p>
          ) : (
            form.items.map((item, index) => (
              <button
                key={item.key}
                type="button"
                className="page-item-chip"
                data-selected={selectedKey === item.key || undefined}
                onClick={() => setSelectedKey(item.key === selectedKey ? null : item.key)}
              >
                <span className="technical">#{index + 1}</span>
                <span>{itemSummary(item)}</span>
                {item.kind === "group_master" && item.tray === false ? (
                  <span className="pill" data-tone="warning">
                    Not adjacent — renders as an ordinary strip (§21.9)
                  </span>
                ) : null}
              </button>
            ))
          )}
        </div>

        {!readOnly ? (
          <div className="flex flex-wrap gap-3">
            <Button variant="secondary" onClick={() => addItem(newChannelItem("mixer"))}>
              + Mixer channel
            </Button>
            <Button variant="secondary" onClick={() => addItem(newChannelItem("lighting"))}>
              + Lighting channel
            </Button>
            <Button variant="secondary" onClick={() => addItem(newGroupMasterItem())}>
              + Group master
            </Button>
            <Button variant="secondary" onClick={() => addItem(newPanelItem())}>
              + Panel
            </Button>
          </div>
        ) : null}
      </div>

      {selected ? (
        <section className="flex flex-col gap-4" aria-label={`Selected — ${itemSummary(selected)}`}>
          <div className="view-head">
            <h2 className="section-title">Selected — {itemSummary(selected)}</h2>
            {!readOnly ? (
              <div className="flex gap-2">
                <Button variant="ghost" onClick={() => moveItem(selected.key, -1)}>
                  Move earlier
                </Button>
                <Button variant="ghost" onClick={() => moveItem(selected.key, 1)}>
                  Move later
                </Button>
                <Button variant="destructive" helpId="pages.item.remove" onClick={() => removeItem(selected.key)}>
                  Remove
                </Button>
              </div>
            ) : null}
          </div>

          {readOnly ? (
            <ReadOnlyItemDetail item={selected} />
          ) : selected.kind === "channel" ? (
            <ChannelItemFields key={selected.key} item={selected} onChange={(next) => replaceItem(selected.key, next)} />
          ) : selected.kind === "group_master" ? (
            <GroupMasterItemFields key={selected.key} item={selected} onChange={(next) => replaceItem(selected.key, next)} />
          ) : (
            <PanelItemFields
              key={selected.key}
              item={selected}
              onChange={(next) => replaceItem(selected.key, next)}
              onOpenButton={(button, col, row) => setButtonSheet({ panelKey: selected.key, button, col, row })}
            />
          )}
        </section>
      ) : null}

      {buttonSheet ? (
        (() => {
          const panel = form.items.find((item) => item.key === buttonSheet.panelKey);
          if (!panel || panel.kind !== "panel") return null;
          const taken = new Set(panel.buttons.map((button) => `${button.col}:${button.row}`));
          return (
            <ButtonEditorSheet
              open
              onOpenChange={(open) => !open && setButtonSheet(null)}
              button={buttonSheet.button}
              initialCol={buttonSheet.col}
              initialRow={buttonSheet.row}
              panelWidth={panel.panelWidth}
              takenCells={taken}
              onSave={(button) => {
                const exists = panel.buttons.some((b) => b.key === button.key);
                const nextButtons = exists ? panel.buttons.map((b) => (b.key === button.key ? button : b)) : [...panel.buttons, button];
                replaceItem(panel.key, { ...panel, buttons: nextButtons });
                setButtonSheet(null);
              }}
              onRemove={
                buttonSheet.button
                  ? () => {
                      replaceItem(panel.key, { ...panel, buttons: panel.buttons.filter((b) => b.key !== buttonSheet.button?.key) });
                      setButtonSheet(null);
                    }
                  : undefined
              }
            />
          );
        })()
      ) : null}

      <ConflictDialog
        open={conflict !== null}
        onOpenChange={(next) => {
          if (!next) setConflict(null);
        }}
        deviceName={`"${page.name}"`}
        rows={
          conflict
            ? diffRecord(
                conflict.current as unknown as Record<string, unknown>,
                { name: form.name, sort_order: form.sortOrder, items: editorItemsToPutItems(form.items) } as unknown as Record<string, unknown>,
              )
            : []
        }
        onReload={() => {
          if (!conflict) return;
          // Write the server's version into the query cache and let the usual
          // "re-derive the form when `updated_at` changes" effect above pick
          // it up, rather than duplicating that logic here.
          client.setQueryData(pagesKeys.detail(page.id), conflict.current);
          setConflict(null);
        }}
        onOverwrite={() => conflict && save(conflict.current.updated_at)}
      />
    </Card>
  );
}

// -- per-kind detail fields ---------------------------------------------------

function ReadOnlyItemDetail({ item }: { item: EditorItem }) {
  if (item.kind === "group_master") {
    return (
      <ul className="scene-log-actions">
        {item.members.map((id) => (
          <li key={id}>Channel #{id}</li>
        ))}
      </ul>
    );
  }
  if (item.kind === "panel") {
    return (
      <ul className="scene-log-actions">
        {item.buttons.map((button) => (
          <li key={button.key}>
            {button.label} ({button.col}, {button.row})
          </li>
        ))}
      </ul>
    );
  }
  return <p className="text-fg-muted text-sm">{item.label}</p>;
}

function ChannelItemFields({ item, onChange }: { item: EditorChannelItem; onChange: (next: EditorChannelItem) => void }) {
  const mixerChannels = useMixerChannels();
  const lightingChannels = useLightingChannels();
  const options: readonly { id: number; name: string }[] = item.source === "mixer" ? (mixerChannels.data ?? []) : (lightingChannels.data?.channels ?? []);

  return (
    <div className="field schema-field">
      <FieldLabel htmlFor="page-item-channel" help="pages.item.channel">
        {item.source === "mixer" ? "Mixer channel" : "Lighting channel"}
      </FieldLabel>
      <Select
        id="page-item-channel"
        value={item.channelId ?? ""}
        onChange={(event) => {
          const id = event.currentTarget.value ? Number(event.currentTarget.value) : null;
          const picked = options.find((option) => option.id === id);
          onChange({ ...item, channelId: id, label: picked?.name ?? "" });
        }}
      >
        <option value="">Choose a channel…</option>
        {options.map((option) => (
          <option key={option.id} value={option.id}>
            {option.name}
          </option>
        ))}
      </Select>
    </div>
  );
}

function GroupMasterItemFields({ item, onChange }: { item: EditorGroupMasterItem; onChange: (next: EditorGroupMasterItem) => void }) {
  const groups = useLightingGroups();
  const lightingChannels = useLightingChannels();
  const groupOptions = groups.data?.groups ?? [];
  const channelById = new Map((lightingChannels.data?.channels ?? []).map((channel) => [channel.id, channel.name]));

  return (
    <div className="flex flex-col gap-4">
      <div className="field schema-field">
        <FieldLabel htmlFor="page-item-group" help="pages.item.group">
          Group
        </FieldLabel>
        <Select
          id="page-item-group"
          value={item.groupId ?? ""}
          onChange={(event) => {
            const id = event.currentTarget.value ? Number(event.currentTarget.value) : null;
            const picked = groupOptions.find((group) => group.id === id);
            onChange({
              ...item,
              groupId: id,
              groupName: picked?.name ?? "",
              groupColour: picked?.colour ?? "",
              members: picked?.channel_ids ?? [],
            });
          }}
        >
          <option value="">Choose a group…</option>
          {groupOptions.map((group) => (
            <option key={group.id} value={group.id}>
              {group.name}
            </option>
          ))}
        </Select>
      </div>

      <Checkbox
        id="page-item-expanded"
        label="Opens expanded"
        checked={item.expanded}
        onChange={(event) => onChange({ ...item, expanded: event.currentTarget.checked })}
      />

      <div>
        <p className="field-label">Members</p>
        <p className="field-help">
          A page never defines membership (§21.9) — the tray fills itself. Edit who belongs to this group on
          Lighting → Groups.
        </p>
        {item.members.length === 0 ? (
          <p className="text-fg-muted text-sm">No members yet.</p>
        ) : (
          <ul className="scene-log-actions">
            {item.members.map((id) => (
              <li key={id}>{channelById.get(id) ?? `Channel #${id}`}</li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

function PanelItemFields({
  item,
  onChange,
  onOpenButton,
}: {
  item: EditorPanelItem;
  onChange: (next: EditorPanelItem) => void;
  onOpenButton: (button: EditorButton | undefined, col: number, row: number) => void;
}) {
  const maxRow = item.buttons.reduce((max, button) => Math.max(max, button.row), -1);
  const [extraRows, setExtraRows] = useState(0);
  const rowCount = Math.max(maxRow + 1, 1) + extraRows;
  const byCell = new Map(item.buttons.map((button) => [`${button.col}:${button.row}`, button]));

  return (
    <div className="flex flex-col gap-4">
      <div className="field schema-field">
        <FieldLabel htmlFor="panel-title" help="pages.item.panel-title">
          Title
        </FieldLabel>
        <Input id="panel-title" value={item.panelTitle} onChange={(event) => onChange({ ...item, panelTitle: event.currentTarget.value })} />
      </div>

      <div className="field schema-field">
        <FieldLabel htmlFor="panel-width" help="pages.item.panel-width">
          Width
        </FieldLabel>
        <Select
          id="panel-width"
          value={item.panelWidth}
          onChange={(event) => onChange({ ...item, panelWidth: clampPanelWidth(Number(event.currentTarget.value)) })}
        >
          {Array.from({ length: MAX_PANEL_WIDTH - MIN_PANEL_WIDTH + 1 }, (_, i) => MIN_PANEL_WIDTH + i).map((width) => (
            <option key={width} value={width}>
              {width} column{width === 1 ? "" : "s"}
            </option>
          ))}
        </Select>
        <p className="field-help">
          Rows are unbounded (§21.9, Q5) — the layout works out how many fit per device; this canvas shows the
          reading order, not a fixed grid.
        </p>
      </div>

      <div>
        <p className="field-label">Buttons</p>
        <div className="page-button-grid" style={{ gridTemplateColumns: `repeat(${item.panelWidth}, 1fr)` }}>
          {Array.from({ length: rowCount }, (_, row) =>
            Array.from({ length: item.panelWidth }, (_, col) => {
              const button = byCell.get(`${col}:${row}`);
              return button ? (
                <button
                  key={`${col}:${row}`}
                  type="button"
                  className="page-button-chip"
                  onClick={() => onOpenButton(button, col, row)}
                >
                  <span className="colour-swatch" aria-hidden="true" style={{ background: button.colour ? groupColourVar(button.colour) : undefined }} />
                  {button.label || "Untitled"}
                </button>
              ) : (
                <button key={`${col}:${row}`} type="button" className="page-button-chip page-button-chip-empty" onClick={() => onOpenButton(undefined, col, row)}>
                  +
                </button>
              );
            }),
          )}
        </div>
        <Button variant="secondary" onClick={() => setExtraRows((n) => n + 1)}>
          + Add row
        </Button>
      </div>
    </div>
  );
}
