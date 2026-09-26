/*
 * The Rules tab (spec §21.17): the list as the mock-up draws it, the editor,
 * Fire and Test, enable/disable, delete with the reference-list 409, and the
 * execution log.
 */
import { AlarmClock, ListChecks, Plus, Zap } from "lucide-react";
import { useMemo, useState } from "react";
import { toast } from "sonner";

import { useDevices } from "@/admin/devices/api";
import type { Device } from "@/admin/devices/types";
import { ApiError } from "@/api/client";
import { Banner } from "@/components/ui/Banner";
import { Button } from "@/components/ui/Button";
import { Checkbox } from "@/components/ui/Select";
import { ConfirmDialog } from "@/components/ui/Sheet";
import { EmptyState, ErrorState, Skeleton } from "@/components/ui/EmptyState";
import { formatDateTime } from "@/admin/updates/format";
import { useLightingGroups } from "@/lighting/api";
import type { LightingGroup } from "@/lighting/types";

import {
  useCreateRule,
  useDeleteRule,
  useFireRule,
  useKnxAddresses,
  useRuleLog,
  useRules,
  useRuleState,
  useScenes,
  useTestRule,
  useUpdateRule,
} from "./api";
import { describeAction, describeTrigger, type DescribeLookups } from "./describe";
import { RuleEditor } from "./RuleEditor";
import { ReferencesDialog, type ReferenceRow } from "./ReferencesDialog";
import type { FireResponse, KnxAddress, Rule, RuleInput, SceneSummary } from "./types";

function statusLine(error: unknown): string | undefined {
  return error instanceof ApiError ? `${error.status} ${error.code}` : undefined;
}

function fireOutcomeToast(response: FireResponse, label: string) {
  if (response.result === "blocked") {
    toast.warning(`${label}: blocked by the guard`);
  } else if (response.fired) {
    toast.success(`${label}: ${response.result}`);
  } else {
    toast.warning(`${label}: did not fire (${response.result})`);
  }
}

interface RuleRowProps {
  rule: Rule;
  state: { state: boolean | null; suppressed: boolean; last_result: string | null; next_fire_at: string | null } | undefined;
  lookups: DescribeLookups;
  isFirst: boolean;
  isLast: boolean;
  onEdit: () => void;
  onMoveUp: () => void;
  onMoveDown: () => void;
  onToggleEnabled: () => void;
  onDelete: () => void;
  reordering: boolean;
}

function RuleRow({ rule, state, lookups, isFirst, isLast, onEdit, onMoveUp, onMoveDown, onToggleEnabled, onDelete, reordering }: RuleRowProps) {
  const fire = useFireRule();
  const test = useTestRule();
  const suppressed = state?.suppressed ?? false;
  const nextFire = rule.trigger_type === "schedule" ? (state?.next_fire_at ?? rule.next_fire_at) : null;

  return (
    <tr data-rule={rule.id}>
      <td className="technical">
        <span className="stage-plan-bar-reorder">
          <Button variant="ghost" size="icon" aria-label={`Move ${rule.name} up`} disabled={reordering || isFirst} onClick={onMoveUp}>
            ↑
          </Button>
          <Button variant="ghost" size="icon" aria-label={`Move ${rule.name} down`} disabled={reordering || isLast} onClick={onMoveDown}>
            ↓
          </Button>
        </span>
      </td>
      <td>
        <button type="button" className="btn btn-ghost" onClick={onEdit}>
          {rule.name}
        </button>
        {rule.note ? (
          <p className="field-note">
            {rule.trigger_type === "schedule" ? <AlarmClock aria-hidden="true" className="size-4" /> : null}
            {rule.note}
          </p>
        ) : null}
        {nextFire ? (
          <p className="field-note">
            <AlarmClock aria-hidden="true" className="size-4" />
            Next: <time dateTime={nextFire}>{formatDateTime(nextFire)}</time>
          </p>
        ) : null}
      </td>
      <td className="technical">{describeTrigger(rule, lookups)}</td>
      <td className="technical">
        {describeAction(rule, lookups)}
        {suppressed ? (
          <span className="pill" data-tone="warning" title="Suppressed while external control is active (§7.2.7)">
            <Zap aria-hidden="true" className="size-4" /> suppressed
          </span>
        ) : null}
      </td>
      <td>
        {state?.state === null || state?.state === undefined ? (
          <span className="technical">—</span>
        ) : (
          <span className="status-dot" data-status={state.state ? "connected" : "unconfigured"} role="img" aria-label={state.state ? "on" : "off"}>
            {state.state ? "● on" : "○ off"}
          </span>
        )}
      </td>
      <td>
        <Checkbox id={`rule-${rule.id}-enabled`} label="Enabled" checked={rule.enabled} onChange={onToggleEnabled} />
      </td>
      <td className="device-actions">
        <Button
          variant="secondary"
          loading={fire.isPending}
          disabled={suppressed}
          onClick={() =>
            fire.mutate(
              { id: rule.id },
              { onSuccess: (r) => fireOutcomeToast(r, "Fired"), onError: (error) => toast.error(error instanceof ApiError ? error.message : "Could not fire") },
            )
          }
        >
          Fire
        </Button>
        <Button
          variant="secondary"
          loading={test.isPending}
          disabled={suppressed}
          onClick={() =>
            test.mutate(
              { id: rule.id },
              { onSuccess: (r) => fireOutcomeToast(r, "Test"), onError: (error) => toast.error(error instanceof ApiError ? error.message : "Could not test") },
            )
          }
        >
          Test
        </Button>
        <Button variant="destructive" helpId="rules.delete" onClick={onDelete}>
          Delete
        </Button>
      </td>
    </tr>
  );
}

export function RulesTab({ visible }: { visible: boolean }) {
  const rules = useRules();
  const ruleState = useRuleState(visible);
  const knxAddresses = useKnxAddresses();
  const scenes = useScenes();
  const lightingGroups = useLightingGroups();
  const devices = useDevices();
  const log = useRuleLog();

  const createRule = useCreateRule();
  const updateRule = useUpdateRule();
  const deleteRule = useDeleteRule();

  const [editing, setEditing] = useState<Rule | "new" | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [confirmDelete, setConfirmDelete] = useState<Rule | null>(null);
  const [references, setReferences] = useState<{ subject: string; message: string; rows: ReferenceRow[] } | null>(null);

  const lookups: DescribeLookups = useMemo(() => {
    const addressById = new Map<number, KnxAddress>((knxAddresses.data ?? []).map((a) => [a.id, a]));
    const deviceNameById = new Map<number, string>((devices.data?.devices ?? []).map((d: Device) => [d.id, d.name]));
    const sceneNameById = new Map<number, string>((scenes.data?.scenes ?? []).map((s: SceneSummary) => [s.id, s.name]));
    const groupNameById = new Map<number, string>((lightingGroups.data?.groups ?? []).map((g: LightingGroup) => [g.id, g.name]));
    return { addressById, deviceNameById, sceneNameById, groupNameById };
  }, [knxAddresses.data, devices.data, scenes.data, lightingGroups.data]);

  const stateByRuleId = useMemo(() => new Map((ruleState.data?.rules ?? []).map((s) => [s.id, s])), [ruleState.data]);

  const orderedRules = useMemo(() => [...(rules.data?.rules ?? [])].sort((a, b) => a.sort_order - b.sort_order || a.id - b.id), [rules.data]);

  function closeEditor() {
    setEditing(null);
    setFieldErrors({});
  }

  async function handleSave(input: RuleInput) {
    setFieldErrors({});
    try {
      if (editing === "new") {
        await createRule.mutateAsync(input);
        toast.success("Rule created");
      } else if (editing) {
        await updateRule.mutateAsync({ id: editing.id, version: editing.updated_at, body: input });
        toast.success("Rule saved");
      }
      closeEditor();
    } catch (error) {
      if (error instanceof ApiError && error.code === "validation_failed") {
        const fields = error.detail["fields"];
        if (fields && typeof fields === "object") {
          const flat: Record<string, string> = {};
          for (const [key, messages] of Object.entries(fields as Record<string, unknown>)) {
            flat[key] = Array.isArray(messages) ? messages.join(" ") : String(messages);
          }
          setFieldErrors(flat);
        }
        return;
      }
      toast.error(error instanceof ApiError ? error.message : "Could not save the rule");
      throw error;
    }
  }

  function requestDelete(rule: Rule) {
    setConfirmDelete(rule);
  }

  function confirmDeleteNow() {
    const rule = confirmDelete;
    if (!rule) return;
    setConfirmDelete(null);
    deleteRule.mutate(rule.id, {
      onSuccess: () => {
        toast.success(`${rule.name} deleted`);
        // Deleted from inside its own editor: close the now-stale sheet too.
        if (editing !== "new" && editing?.id === rule.id) closeEditor();
      },
      onError: (error) => {
        if (error instanceof ApiError && error.code === "in_use") {
          const refs = error.detail["references"];
          setReferences({
            subject: rule.name,
            message: error.message,
            rows: Array.isArray(refs) ? (refs as ReferenceRow[]) : [],
          });
          return;
        }
        toast.error(error instanceof ApiError ? error.message : "Could not delete the rule");
      },
    });
  }

  function toggleEnabled(rule: Rule) {
    updateRule.mutate(
      { id: rule.id, version: rule.updated_at, body: { enabled: !rule.enabled } },
      { onError: (error) => toast.error(error instanceof ApiError ? error.message : "Could not change the rule") },
    );
  }

  function move(rule: Rule, direction: -1 | 1) {
    const index = orderedRules.findIndex((r) => r.id === rule.id);
    const neighbour = orderedRules[index + direction];
    if (!neighbour) return;
    updateRule.mutate(
      { id: rule.id, version: rule.updated_at, body: { sort_order: neighbour.sort_order } },
      { onError: (error) => toast.error(error instanceof ApiError ? error.message : "Could not reorder") },
    );
    updateRule.mutate(
      { id: neighbour.id, version: neighbour.updated_at, body: { sort_order: rule.sort_order } },
      { onError: (error) => toast.error(error instanceof ApiError ? error.message : "Could not reorder") },
    );
  }

  if (rules.isPending) {
    return (
      <div aria-busy="true" aria-label="Loading the rules">
        <Skeleton className="h-8 w-full" />
        <Skeleton className="h-touch w-full" />
      </div>
    );
  }
  if (rules.isError) {
    return (
      <ErrorState
        title="Could not load the rules"
        detail="The controller did not answer. Nothing has been changed."
        status={statusLine(rules.error)}
        onRetry={() => void rules.refetch()}
      />
    );
  }

  const editingRule = editing === "new" ? undefined : (editing ?? undefined);

  return (
    // A <section>, not a <div>: its <header> below has no landmark role
    // nested in a sectioning root, whereas a plain <div> gave it the
    // implicit "banner" role — a second one alongside the screen's own
    // top-level header (axe: landmark-no-duplicate-banner, §24.3).
    <section className="card device-card">
      <header className="device-head">
        <h2 className="card-title">Rules</h2>
        <Button variant="primary" helpId="rules.add" onClick={() => setEditing("new")}>
          <Plus aria-hidden="true" className="size-4" /> Add rule
        </Button>
      </header>

      {orderedRules.length === 0 ? (
        <EmptyState
          icon={ListChecks}
          title="No rules yet"
          detail="A rule says when something happens and what it should do — a KNX telegram, a schedule, or a device changing state."
          action={
            <Button variant="primary" helpId="rules.add" onClick={() => setEditing("new")}>
              Add the first rule
            </Button>
          }
        />
      ) : (
        <div className="device-section" style={{ overflowX: "auto" }}>
          <table className="diff-table">
            <thead>
              <tr>
                <th scope="col" aria-label="Reorder" />
                <th scope="col">Name</th>
                <th scope="col">When</th>
                <th scope="col">Then</th>
                <th scope="col">State</th>
                <th scope="col">Enabled</th>
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {orderedRules.map((rule, index) => (
                <RuleRow
                  key={rule.id}
                  rule={rule}
                  state={stateByRuleId.get(rule.id)}
                  lookups={lookups}
                  isFirst={index === 0}
                  isLast={index === orderedRules.length - 1}
                  reordering={updateRule.isPending}
                  onEdit={() => setEditing(rule)}
                  onMoveUp={() => move(rule, -1)}
                  onMoveDown={() => move(rule, 1)}
                  onToggleEnabled={() => toggleEnabled(rule)}
                  onDelete={() => requestDelete(rule)}
                />
              ))}
            </tbody>
          </table>
          <p className="field-help">All matching rules run, in this order (§8.7).</p>
        </div>
      )}

      {log.data && log.data.entries.length > 0 ? (
        <section className="device-section" aria-labelledby="rules-log">
          <h3 className="sect-label" id="rules-log">
            Execution log
          </h3>
          <div style={{ overflowX: "auto" }}>
            <table className="diff-table">
              <thead>
                <tr>
                  <th scope="col">Fired</th>
                  <th scope="col">Rule</th>
                  <th scope="col">Trigger</th>
                  <th scope="col">Guard</th>
                  <th scope="col">Outcome</th>
                </tr>
              </thead>
              <tbody>
                {log.data.entries.map((entry) => (
                  <tr key={entry.id}>
                    <td className="technical">{entry.fired_at}</td>
                    <td>{orderedRules.find((r) => r.id === entry.rule_id)?.name ?? entry.rule_id ?? "—"}</td>
                    <td className="technical">{entry.triggered_by}</td>
                    <td>{entry.guard_result ?? "—"}</td>
                    <td>{entry.result ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      ) : null}

      {editing ? (
        <RuleEditor
          key={editing === "new" ? "new" : editing.id}
          open={true}
          onOpenChange={(open) => {
            if (!open) closeEditor();
          }}
          rule={editingRule}
          knxAddresses={knxAddresses.data ?? []}
          scenes={scenes.data?.scenes ?? []}
          lightingGroups={lightingGroups.data?.groups ?? []}
          devices={devices.data?.devices ?? []}
          saving={createRule.isPending || updateRule.isPending}
          deleting={deleteRule.isPending}
          onSave={handleSave}
          onDelete={editingRule ? () => requestDelete(editingRule) : undefined}
          fieldErrors={fieldErrors}
          onErrorsHandled={() => setFieldErrors({})}
        />
      ) : null}

      <ConfirmDialog
        open={confirmDelete !== null}
        onOpenChange={(open) => {
          if (!open) setConfirmDelete(null);
        }}
        title={`Delete ${confirmDelete?.name ?? "this rule"}?`}
        description="This cannot be undone. Anything that fires this rule will stop working."
        confirmLabel="Delete"
        destructive
        onConfirm={confirmDeleteNow}
      />

      {references ? (
        <ReferencesDialog
          open={true}
          onOpenChange={(open) => {
            if (!open) setReferences(null);
          }}
          subject={references.subject}
          message={references.message}
          references={references.rows}
        />
      ) : (
        <ReferencesDialog open={false} onOpenChange={() => undefined} subject="" message="" references={[]} />
      )}

      {ruleState.data?.external_control ? (
        <Banner tone="warning" title="External control is active">
          Every lighting_group rule is suppressed and its Test is disabled (§7.2.7).
        </Banner>
      ) : null}
    </section>
  );
}
