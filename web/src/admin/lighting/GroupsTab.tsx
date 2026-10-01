/*
 * Admin → Lighting → Groups (spec §21.18 *Groups tab*): each group with its
 * colour accent and member list, opening the same group sheet (§21.18: "one
 * group sheet") the stage plan's multi-select "Group" action uses, here in
 * its edit mode.
 */
import { useState } from "react";
import { Boxes } from "lucide-react";

import { ApiError } from "@/api/client";
import { presentError } from "@/api/errors";
import { Button } from "@/components/ui/Button";
import { EmptyState, ErrorState, Skeleton } from "@/components/ui/EmptyState";
import { colourToCss } from "@/lighting/colour";
import { useLightingChannels, useLightingGroups } from "@/lighting/api";
import type { LightingGroup } from "@/lighting/types";
import { GroupSheet } from "@/stageplan/GroupSheet";

/** `lighting_groups.colour`'s own schema default (§15.9), built from RGB components rather than a literal (token discipline, `discipline.test.ts`). */
const DEFAULT_GROUP_COLOUR = colourToCss({ r: 46, g: 134, b: 193, w: null });

import { useCreateGroupFull, useDeleteGroup, useUpdateGroup } from "./api";

function statusLine(error: unknown): string | undefined {
  return error instanceof ApiError ? `${error.status} ${error.code}` : undefined;
}

export function GroupsTab() {
  const groups = useLightingGroups();
  const channels = useLightingChannels();
  const [editing, setEditing] = useState<LightingGroup | null>(null);
  const [sheetSeq, setSheetSeq] = useState(0);

  const updateGroup = useUpdateGroup();
  const deleteGroup = useDeleteGroup();
  const createGroup = useCreateGroupFull();

  if (groups.isPending || channels.isPending) {
    return (
      <div className="flex flex-col gap-4" aria-busy="true" aria-label="Loading groups">
        <Skeleton className="h-touch w-full" />
      </div>
    );
  }

  if (groups.isError || channels.isError) {
    return (
      <ErrorState
        title="Could not load groups"
        detail="The controller did not answer. Nothing has been changed."
        status={statusLine(groups.error ?? channels.error)}
        onRetry={() => {
          void groups.refetch();
          void channels.refetch();
        }}
      />
    );
  }

  const allGroups = groups.data?.groups ?? [];
  const allChannels = channels.data?.channels ?? [];
  const nameOf = new Map(allChannels.map((c) => [c.id, c.name] as const));

  return (
    <div className="lighting-panel">
      <div className="flex justify-end">
        <Button
          variant="primary"
          helpId="lighting.groups.add"
          onClick={() => {
            createGroup.mutate(
              { name: "New group", colour: DEFAULT_GROUP_COLOUR, sort_order: allGroups.length, channel_ids: [] },
              { onError: (error) => presentError(error) },
            );
          }}
          loading={createGroup.isPending}
        >
          Add group
        </Button>
      </div>

      {allGroups.length === 0 ? (
        <EmptyState icon={Boxes} title="No groups yet" detail="A group is a set of fixtures with one shared fader (§9.4)." />
      ) : (
        <ul className="flex flex-col gap-2">
          {allGroups.map((group) => (
            <li key={group.id} className="card flex flex-row items-center justify-between gap-3">
              <div className="flex items-center gap-3">
                <span aria-hidden="true" className="colour-swatch" style={{ background: group.colour }} />
                <div>
                  <p className="card-title">{group.name}</p>
                  {group.indicator_only ? <p className="text-fg-muted text-sm">Indicator only — no fader; used for panel status lights</p> : null}
                  <p className="text-fg-muted text-sm">
                    {group.channel_ids.length === 0
                      ? "No members"
                      : group.channel_ids
                          .map((id) => nameOf.get(id) ?? `#${id}`)
                          .slice(0, 4)
                          .join(", ") + (group.channel_ids.length > 4 ? `, +${group.channel_ids.length - 4} more` : "")}
                  </p>
                </div>
              </div>
              <Button
                variant="secondary"
                onClick={() => {
                  setSheetSeq((seq) => seq + 1);
                  setEditing(group);
                }}
              >
                Edit
              </Button>
            </li>
          ))}
        </ul>
      )}

      {editing ? (
        <GroupSheet
          key={`group-edit-${sheetSeq}`}
          mode="edit"
          open
          onOpenChange={(open) => !open && setEditing(null)}
          group={editing}
          fixtures={allChannels}
          saving={updateGroup.isPending}
          onSave={(input) =>
            updateGroup.mutate(
              { id: editing.id, version: editing.updated_at, ...input },
              { onSuccess: () => setEditing(null), onError: (error) => presentError(error) },
            )
          }
          deleting={deleteGroup.isPending}
          onDelete={() =>
            deleteGroup.mutate(editing.id, { onSuccess: () => setEditing(null), onError: (error) => presentError(error) })
          }
        />
      ) : null}
    </div>
  );
}
