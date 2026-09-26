/*
 * Admin → Lighting → Fixture profiles (spec §21.18, §15.9): manufacturer,
 * model, name and channel count, with duplicate-and-edit — "a hand-entered
 * profile takes two minutes, starting from a duplicate" (§15.9).
 */
import { useState } from "react";
import { Copy, ListTree } from "lucide-react";

import { ApiError } from "@/api/client";
import { presentError } from "@/api/errors";
import { Banner } from "@/components/ui/Banner";
import { Button } from "@/components/ui/Button";
import { EmptyState, ErrorState, Skeleton } from "@/components/ui/EmptyState";
import { useLightingChannels } from "@/lighting/api";
import type { FixtureProfile } from "@/lighting/types";

import { useCreateProfile, useDeleteProfile, useFixtureProfiles, useUpdateProfile } from "./api";
import { ProfileEditor, type ProfileEditorInput } from "./ProfileEditor";

function statusLine(error: unknown): string | undefined {
  return error instanceof ApiError ? `${error.status} ${error.code}` : undefined;
}

export function ProfilesTab() {
  const profiles = useFixtureProfiles();
  const channels = useLightingChannels();
  const [editing, setEditing] = useState<{ profile: FixtureProfile | null } | null>(null);
  const [sheetSeq, setSheetSeq] = useState(0);
  const [inUse, setInUse] = useState<{ profileName: string; references: readonly { name: string }[] } | null>(null);

  const createProfile = useCreateProfile();
  const updateProfile = useUpdateProfile();
  const deleteProfile = useDeleteProfile();

  if (profiles.isPending || channels.isPending) {
    return (
      <div className="flex flex-col gap-4" aria-busy="true" aria-label="Loading fixture profiles">
        <Skeleton className="h-touch w-full" />
      </div>
    );
  }

  if (profiles.isError || channels.isError) {
    return (
      <ErrorState
        title="Could not load fixture profiles"
        detail="The controller did not answer. Nothing has been changed."
        status={statusLine(profiles.error ?? channels.error)}
        onRetry={() => {
          void profiles.refetch();
          void channels.refetch();
        }}
      />
    );
  }

  const all = profiles.data?.profiles ?? [];
  const allChannels = channels.data?.channels ?? [];
  const usageOf = (profileId: number): number => allChannels.filter((c) => c.profile_id === profileId).length;

  function save(input: ProfileEditorInput): void {
    const target = editing?.profile;
    const body = { name: input.name, manufacturer: input.manufacturer, model: input.model, channel_count: input.channels.length, channels: input.channels };
    if (target) {
      updateProfile.mutate(
        { id: target.id, version: target.updated_at, ...body },
        { onSuccess: () => setEditing(null), onError: (error) => presentError(error) },
      );
    } else {
      createProfile.mutate(body, { onSuccess: () => setEditing(null), onError: (error) => presentError(error) });
    }
  }

  function duplicate(profile: FixtureProfile): void {
    createProfile.mutate(
      {
        name: `${profile.name} (copy)`,
        manufacturer: profile.manufacturer,
        model: profile.model,
        channel_count: profile.channel_count,
        channels: profile.channels.map((c) => ({ offset: c.offset, role: c.role, default: c.default })),
      },
      {
        onSuccess: (created) => {
          // §15.9: duplicate-and-edit opens the copy straight away.
          setSheetSeq((seq) => seq + 1);
          setEditing({ profile: created });
        },
        onError: (error) => presentError(error),
      },
    );
  }

  function remove(profile: FixtureProfile): void {
    deleteProfile.mutate(profile.id, {
      onError: (error) => {
        if (error instanceof ApiError && error.code === "in_use") {
          const refs = error.detail["references"];
          setInUse({
            profileName: profile.name,
            references: Array.isArray(refs) ? refs.map((r) => ({ name: typeof r?.name === "string" ? r.name : String(r) })) : [],
          });
          return;
        }
        presentError(error);
      },
    });
  }

  return (
    <div className="lighting-panel">
      <div className="flex justify-end">
        <Button
          variant="primary"
          helpId="lighting.profiles.add"
          onClick={() => {
            setSheetSeq((seq) => seq + 1);
            setEditing({ profile: null });
          }}
        >
          Add profile
        </Button>
      </div>

      {inUse ? (
        <Banner tone="danger" title={`${inUse.profileName} is still in use`}>
          Seeded profiles cannot be deleted while fixtures use them.
          {inUse.references.length > 0 ? ` Used by: ${inUse.references.map((r) => r.name).join(", ")}.` : ""}
        </Banner>
      ) : null}

      {all.length === 0 ? (
        <EmptyState icon={ListTree} title="No fixture profiles yet" detail="A profile describes what each of a fixture's channels does (§15.9)." />
      ) : (
        <div className="lighting-table-scroll">
          <table className="lighting-table">
            <caption className="sr-only">Fixture profiles</caption>
            <thead>
              <tr>
                <th scope="col">Name</th>
                <th scope="col">Manufacturer</th>
                <th scope="col">Model</th>
                <th scope="col">Channels</th>
                <th scope="col">In use</th>
                <th scope="col">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {all.map((profile) => (
                <tr key={profile.id}>
                  <td>
                    <button
                      type="button"
                      className="btn btn-ghost"
                      onClick={() => {
                        setSheetSeq((seq) => seq + 1);
                        setEditing({ profile });
                      }}
                    >
                      {profile.name}
                    </button>
                  </td>
                  <td>{profile.manufacturer ?? "—"}</td>
                  <td>{profile.model ?? "—"}</td>
                  <td className="technical">{profile.channel_count}</td>
                  <td className="technical">{usageOf(profile.id)}</td>
                  <td>
                    <div className="flex gap-2">
                      <Button variant="ghost" size="icon" aria-label={`Duplicate ${profile.name}`} onClick={() => duplicate(profile)}>
                        <Copy aria-hidden="true" className="size-4" />
                      </Button>
                      <Button variant="destructive" helpId="lighting.profiles.delete" onClick={() => remove(profile)}>
                        Delete
                      </Button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {editing ? (
        <ProfileEditor
          key={`profile-${sheetSeq}`}
          open
          onOpenChange={(open) => !open && setEditing(null)}
          profile={editing.profile}
          fixturesUsingCount={editing.profile ? usageOf(editing.profile.id) : 0}
          saving={createProfile.isPending || updateProfile.isPending}
          onSave={save}
        />
      ) : null}
    </div>
  );
}
