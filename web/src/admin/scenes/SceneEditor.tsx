/*
 * The scene editor (spec §21.16): scene details, the read-only "what
 * triggers this scene" panel (which doubles as the delete guard, §8.11), the
 * timeline of actions grouped by delay (§8.13), and testing.
 *
 * "A scene is a *what*; deciding *when* is a rule (§8.1)" — there is no
 * trigger editor here, only the list of rules that already point at this
 * scene, each a plain reference from `GET /scenes/{id}/references`.
 */
import { ArrowDown, ArrowUp, ChevronLeft, ListPlus, Pencil, Trash2 } from "lucide-react";
import { useState, type KeyboardEvent } from "react";

import { ApiError } from "@/api/client";
import { presentError } from "@/api/errors";
import { Button } from "@/components/ui/Button";
import { Checkbox, Select } from "@/components/ui/Select";
import { EmptyState, ErrorState, Skeleton } from "@/components/ui/EmptyState";
import { Field, Input } from "@/components/ui/Input";
import { FieldLabel } from "@/help/HelpButton";
import { saveOnShortcut } from "@/lib/keyboard";

import { ActionEditorSheet } from "./ActionEditorSheet";
import { useDeleteAction, useSceneDetail, useSceneDomains, useSceneReferences, useTestSceneGroup, useUpdateAction, useUpdateScene } from "./api";
import { LogViewer } from "./LogViewer";
import { TestPanel } from "./TestPanel";
import { defaultDelayMs, groupByDelay, nextSortOrder } from "./timeline";
import { DOMAIN_LABELS, type Action, type Domain, type RunResult, type ScenePriority } from "./types";
import { UnsavedChangesDialog } from "./UnsavedChangesDialog";
import { useCriticalTestGate } from "./useCriticalTestGate";

export interface SceneEditorProps {
  sceneId: number;
  onBack: () => void;
}

interface FormValues {
  name: string;
  description: string;
  icon: string;
  priority: ScenePriority;
  enabled: boolean;
  visibleOperator: boolean;
}

function actionSummary(action: Action): string {
  switch (action.domain) {
    case "dmx": {
      const count = Object.keys(action.dmx_snapshot ?? {}).length;
      const fade = action.dmx_fade_ms ? `, ${action.dmx_fade_ms}ms fade` : "";
      return `${count} channel${count === 1 ? "" : "s"}${fade}`;
    }
    case "knx":
      return action.knx_source === "literal" ? `Write ${action.knx_value ?? ""}` : "Write the trigger's value";
    default:
      return "";
  }
}

export function SceneEditor({ sceneId, onBack }: SceneEditorProps) {
  const detail = useSceneDetail(sceneId);
  const domains = useSceneDomains();
  const references = useSceneReferences(sceneId);
  const updateScene = useUpdateScene();
  const updateAction = useUpdateAction();
  const deleteAction = useDeleteAction();
  const testGroup = useTestSceneGroup();

  const [form, setForm] = useState<FormValues | null>(null);
  const [syncedVersion, setSyncedVersion] = useState<string | undefined>(undefined);
  const [errors, setErrors] = useState<Readonly<Record<string, string>>>({});
  const [actionSheet, setActionSheet] = useState<{ action?: Action; delayMs: number; sortOrder: number } | null>(null);
  const [testResult, setTestResult] = useState<RunResult | null>(null);
  const [logOpen, setLogOpen] = useState(false);
  const [guardOpen, setGuardOpen] = useState(false);
  // Announces a keyboard reorder (§24.2): "Action moved to position 3 of 6."
  const [moveAnnouncement, setMoveAnnouncement] = useState("");

  const scene = detail.data;
  const groupTestGate = useCriticalTestGate(scene?.priority === "critical");

  // Re-derive the form only when the scene's own `updated_at` changes — not
  // on every refetch, which also fires after an unrelated action edit and
  // would otherwise wipe out whatever the admin is mid-typing here. Storing
  // the previous version and comparing during render (rather than in an
  // effect) is React's documented way to adjust state from data that just
  // arrived, without an extra render pass.
  if (scene && scene.updated_at !== syncedVersion) {
    setSyncedVersion(scene.updated_at);
    setForm({
      name: scene.name,
      description: scene.description ?? "",
      icon: scene.icon ?? "",
      priority: scene.priority,
      enabled: scene.enabled,
      visibleOperator: scene.visible_operator,
    });
  }

  if (detail.isPending || !form) {
    return (
      <div className="view" aria-busy="true" aria-label="Loading the scene">
        <Skeleton className="h-touch w-full" />
        <Skeleton className="h-touch w-full" />
      </div>
    );
  }

  if (detail.isError || !scene) {
    return (
      <div className="view">
        <ErrorState title="Could not load this scene" status={detail.error instanceof ApiError ? `${detail.error.status} ${detail.error.code}` : undefined} onRetry={() => void detail.refetch()} onBack={onBack} />
      </div>
    );
  }

  const dirty =
    form.name !== scene.name ||
    form.description !== (scene.description ?? "") ||
    form.icon !== (scene.icon ?? "") ||
    form.priority !== scene.priority ||
    form.enabled !== scene.enabled ||
    form.visibleOperator !== scene.visible_operator;

  function requestBack() {
    if (dirty) setGuardOpen(true);
    else onBack();
  }

  function save(onDone?: () => void) {
    if (!form || !scene) return;
    const trimmed = form.name.trim();
    if (!trimmed) {
      setErrors({ name: "Give the scene a name" });
      return;
    }
    updateScene.mutate(
      {
        id: sceneId,
        version: scene.updated_at,
        body: {
          name: trimmed,
          description: form.description.trim() || null,
          icon: form.icon.trim() || null,
          priority: form.priority,
          enabled: form.enabled,
          visible_operator: form.visibleOperator,
        },
      },
      {
        onSuccess: () => {
          setErrors({});
          onDone?.();
        },
        onError: (error) => {
          if (error instanceof ApiError && error.code === "validation_failed") {
            const presentation = presentError(error);
            if (presentation?.kind === "inline") setErrors(presentation.fields);
            return;
          }
          presentError(error);
        },
      },
    );
  }

  const actions = [...scene.actions];
  const groups = groupByDelay(actions);

  function moveAction(group: readonly Action[], index: number, direction: -1 | 1) {
    const target = index + direction;
    if (target < 0 || target >= group.length) return;
    const a = group[index];
    const b = group[target];
    if (!a || !b) return;
    updateAction.mutate({ sceneId, actionId: a.id, version: a.updated_at, body: { sort_order: b.sort_order } });
    updateAction.mutate({ sceneId, actionId: b.id, version: b.updated_at, body: { sort_order: a.sort_order } });
    setMoveAnnouncement(`Action moved to position ${target + 1} of ${group.length}.`);
  }

  // Arrow Up/Down reorders the focused action card within its delay group,
  // the keyboard equivalent of the Move earlier/later buttons (§24.2).
  function onActionCardKeyDown(event: KeyboardEvent<HTMLDivElement>, group: readonly Action[], index: number) {
    if (event.key === "ArrowUp") {
      event.preventDefault();
      moveAction(group, index, -1);
    } else if (event.key === "ArrowDown") {
      event.preventDefault();
      moveAction(group, index, 1);
    }
  }

  return (
    <div className="view" onKeyDown={saveOnShortcut(() => save())}>
      <header className="view-head">
        <div className="flex items-center gap-3">
          <Button variant="ghost" size="icon" aria-label="Back to scenes" onClick={requestBack}>
            <ChevronLeft aria-hidden="true" className="size-5" />
          </Button>
          <h1 className="view-title">{scene.name}</h1>
          {!scene.enabled ? <span className="pill">Disabled</span> : null}
        </div>
        <div className="flex gap-3">
          <Button variant="secondary" onClick={() => setLogOpen(true)}>
            Execution log
          </Button>
          <Button variant="primary" helpId="scenes.save" loading={updateScene.isPending} onClick={() => save()}>
            Save
          </Button>
        </div>
      </header>

      <div className="grid grid-cols-1 desktop:grid-cols-2 gap-6">
        <section className="flex flex-col gap-4">
          <h2 className="section-title">Scene details</h2>
          <Field label="Name" htmlFor="scene-name" helpId="scenes.name" error={errors["name"]} errorId="scene-name-error">
            <Input id="scene-name" value={form.name} onChange={(event) => setForm({ ...form, name: event.currentTarget.value })} />
          </Field>
          <Field label="Description" htmlFor="scene-description" helpId="scenes.description" error={errors["description"]} errorId="scene-description-error">
            <Input id="scene-description" value={form.description} onChange={(event) => setForm({ ...form, description: event.currentTarget.value })} />
          </Field>
          <Field label="Icon" htmlFor="scene-icon" helpId="scenes.icon" error={errors["icon"]} errorId="scene-icon-error">
            <Input id="scene-icon" value={form.icon} onChange={(event) => setForm({ ...form, icon: event.currentTarget.value })} placeholder="🎭" />
          </Field>
          <div className="field schema-field">
            <FieldLabel htmlFor="scene-priority" help="scenes.priority">
              Priority
            </FieldLabel>
            <Select id="scene-priority" value={form.priority} onChange={(event) => setForm({ ...form, priority: event.currentTarget.value as ScenePriority })}>
              <option value="normal">Normal</option>
              <option value="critical">Critical</option>
            </Select>
          </div>
          <Checkbox
            id="scene-enabled"
            label="Enabled"
            checked={form.enabled}
            onChange={(event) => setForm({ ...form, enabled: event.currentTarget.checked })}
          />
          <Checkbox
            id="scene-visible-operator"
            label="Visible to operators"
            checked={form.visibleOperator}
            onChange={(event) => setForm({ ...form, visibleOperator: event.currentTarget.checked })}
          />

          <h2 className="section-title">Triggers</h2>
          {references.data && references.data.references.length > 0 ? (
            <ul className="scene-log-actions">
              {references.data.references.map((reference) => (
                <li key={`${reference.entity}-${reference.id}`}>
                  {reference.name} ({reference.entity})
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-fg-muted text-sm">No rule triggers this scene yet (§8.1).</p>
          )}

          <TestPanel sceneId={sceneId} sceneName={scene.name} critical={scene.priority === "critical"} result={testResult} onResult={setTestResult} />
        </section>

        <section className="flex flex-col gap-4">
          <div className="view-head">
            <h2 className="section-title">Actions</h2>
            <Button
              variant="secondary"
              onClick={() =>
                setActionSheet({ delayMs: defaultDelayMs(actions), sortOrder: nextSortOrder(actions, defaultDelayMs(actions)) })
              }
            >
              + Add
            </Button>
          </div>
          <div aria-live="polite" role="status" className="sr-only">
            {moveAnnouncement}
          </div>

          {groups.length === 0 ? (
            <EmptyState icon={ListPlus} title="No actions yet" detail="Add an action to build this scene (§8.12)." />
          ) : (
            <div className="scene-timeline">
              {groups.map((group) => (
                <div key={group.delayMs} className="scene-timeline-group">
                  <div className="scene-timeline-group-head">
                    <span className="scene-timeline-delay">{group.delayMs} ms</span>
                    <Button
                      variant="secondary"
                      loading={testGroup.isPending}
                      onClick={() =>
                        groupTestGate.requestRun(() =>
                          testGroup.mutate(
                            { sceneId, delayMs: group.delayMs },
                            { onSuccess: setTestResult, onError: (error) => void presentError(error) },
                          ),
                        )
                      }
                    >
                      Test group
                    </Button>
                  </div>
                  {group.actions.map((action, index) => (
                    <div
                      key={action.id}
                      className="scene-action-row"
                      tabIndex={0}
                      role="group"
                      aria-label={`${DOMAIN_LABELS[action.domain as Domain] ?? action.domain} action, position ${index + 1} of ${group.actions.length}`}
                      onKeyDown={(event) => onActionCardKeyDown(event, group.actions, index)}
                    >
                      <span className="scene-action-domain">{DOMAIN_LABELS[action.domain as Domain] ?? action.domain}</span>
                      <span className="scene-action-summary">{actionSummary(action)}</span>
                      <div className="scene-action-actions">
                        <Button variant="ghost" size="icon" aria-label="Move earlier" disabled={index === 0} onClick={() => moveAction(group.actions, index, -1)}>
                          <ArrowUp aria-hidden="true" className="size-4" />
                        </Button>
                        <Button
                          variant="ghost"
                          size="icon"
                          aria-label="Move later"
                          disabled={index === group.actions.length - 1}
                          onClick={() => moveAction(group.actions, index, 1)}
                        >
                          <ArrowDown aria-hidden="true" className="size-4" />
                        </Button>
                        <Button variant="ghost" size="icon" aria-label="Edit action" onClick={() => setActionSheet({ action, delayMs: action.delay_ms, sortOrder: action.sort_order })}>
                          <Pencil aria-hidden="true" className="size-4" />
                        </Button>
                        <Button variant="ghost" size="icon" aria-label="Remove action" onClick={() => deleteAction.mutate({ sceneId, actionId: action.id })}>
                          <Trash2 aria-hidden="true" className="size-4" />
                        </Button>
                      </div>
                    </div>
                  ))}
                </div>
              ))}
            </div>
          )}
        </section>
      </div>

      {actionSheet ? (
        <ActionEditorSheet
          open={actionSheet !== null}
          onOpenChange={(open) => {
            if (!open) setActionSheet(null);
          }}
          sceneId={sceneId}
          action={actionSheet.action}
          defaultDelayMs={actionSheet.delayMs}
          defaultSortOrder={actionSheet.sortOrder}
          availability={domains.data?.domains}
        />
      ) : null}

      <UnsavedChangesDialog
        open={guardOpen}
        onOpenChange={setGuardOpen}
        sceneName={scene.name}
        saving={updateScene.isPending}
        onSaveAndLeave={() => save(() => { setGuardOpen(false); onBack(); })}
        onDiscard={() => {
          setForm({
            name: scene.name,
            description: scene.description ?? "",
            icon: scene.icon ?? "",
            priority: scene.priority,
            enabled: scene.enabled,
            visibleOperator: scene.visible_operator,
          });
          setGuardOpen(false);
          onBack();
        }}
      />

      <LogViewer open={logOpen} onOpenChange={setLogOpen} sceneId={sceneId} />
    </div>
  );
}
