/*
 * Saving a fixture — create or update, wherever the fixture sheet is hosted
 * (the Stage Plan tab, the Fixtures tab): §16.1's `validation_failed` lands
 * on the sheet's fields, and `conflict` is handled the way the Devices
 * screen handles it (task brief) — reload-or-overwrite with a diff, reusing
 * `admin/devices/ConflictDialog` and `diffRecord` rather than building a
 * second one. Pulled out of both callers so the handling exists once.
 */
import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { ApiError } from "@/api/client";
import { presentError } from "@/api/errors";
import { lightingKeys } from "@/lighting/api";
import type { LightingChannel } from "@/lighting/types";
import type { FixtureSheetInput } from "@/stageplan/FixtureSheet";

import { useCreateFixture, useUpdateFixture } from "./api";
import { diffRecord, type ConflictRow } from "./diff";

export interface FixtureConflict {
  rows: readonly ConflictRow[];
}

export interface FixtureSaveState {
  saving: boolean;
  fieldErrors: Readonly<Record<string, string>>;
  conflict: FixtureConflict | null;
  save: (editing: LightingChannel | null, input: FixtureSheetInput) => void;
  /** Keep the fixture sheet's typed values and reload the record everyone else already sees. */
  reloadConflict: () => void;
  /** Save what was typed anyway, replacing what the other write landed. */
  overwriteConflict: () => void;
}

export function useFixtureSave(onSaved: () => void): FixtureSaveState {
  const client = useQueryClient();
  const createFixture = useCreateFixture();
  const updateFixture = useUpdateFixture();
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [conflict, setConflict] = useState<{ rows: ConflictRow[]; editingId: number; version: string; pending: FixtureSheetInput } | null>(
    null,
  );

  function handleError(error: unknown, editing: LightingChannel | null, input: FixtureSheetInput): void {
    if (error instanceof ApiError && error.code === "validation_failed") {
      const presentation = presentError(error);
      if (presentation?.kind === "inline") {
        setFieldErrors(presentation.fields);
        return;
      }
    }
    if (error instanceof ApiError && error.code === "conflict" && editing) {
      const current = (error.detail["current"] as LightingChannel | undefined) ?? editing;
      setConflict({
        rows: diffRecord(current as unknown as Record<string, unknown>, input as unknown as Record<string, unknown>),
        editingId: editing.id,
        version: current.updated_at,
        pending: input,
      });
      return;
    }
    presentError(error);
  }

  function save(editing: LightingChannel | null, input: FixtureSheetInput): void {
    setFieldErrors({});
    if (editing) {
      updateFixture.mutate(
        { id: editing.id, version: editing.updated_at, ...input },
        { onSuccess: onSaved, onError: (error) => handleError(error, editing, input) },
      );
    } else {
      createFixture.mutate(input, { onSuccess: onSaved, onError: (error) => handleError(error, editing, input) });
    }
  }

  function reloadConflict(): void {
    setConflict(null);
    // The reload IS the fresh record: invalidating refetches it into every
    // open query, and the caller remounts the sheet from that fresh data.
    void client.invalidateQueries({ queryKey: lightingKeys.channels });
  }

  function overwriteConflict(): void {
    if (!conflict) return;
    const { editingId, version, pending } = conflict;
    setConflict(null);
    updateFixture.mutate(
      { id: editingId, version, ...pending },
      {
        onSuccess: onSaved,
        onError: (error) =>
          handleError(error, { id: editingId, updated_at: version } as LightingChannel, pending),
      },
    );
  }

  return {
    saving: createFixture.isPending || updateFixture.isPending,
    fieldErrors,
    conflict: conflict ? { rows: conflict.rows } : null,
    save,
    reloadConflict,
    overwriteConflict,
  };
}
