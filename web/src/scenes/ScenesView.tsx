/*
 * The operator Scenes view (spec §21.10). A grid of scene cards for every
 * scene visible to the caller's tier — admins see the same view operators
 * do, and `GET /scenes` already filters by tier server-side (§16.5), so this
 * component draws whatever it is handed. Hirers are Phase 5 and are not
 * wired in here (§15.12 page assignment does not exist yet).
 */
import { Sparkles } from "lucide-react";
import { useEffect, useRef } from "react";

import { ApiError } from "@/api/client";
import { presentError } from "@/api/errors";
import { EmptyState, ErrorState, Skeleton } from "@/components/ui/EmptyState";
import { getScenes, subscribeScenes } from "@/live/store";

import { useRefreshScenes, useScenesList, useTriggerScene } from "./api";
import { SceneCard } from "./SceneCard";
import type { Scene } from "./types";

function statusLine(error: unknown): string | undefined {
  return error instanceof ApiError ? `${error.status} ${error.code}` : undefined;
}

/** Refetches the scene list once a run completes, so "last ran" catches up. */
function useRefreshOnCompletion(): void {
  const refresh = useRefreshScenes();
  const seen = useRef(getScenes().lastResult);
  useEffect(
    () =>
      subscribeScenes(() => {
        const result = getScenes().lastResult;
        if (result && result !== seen.current) {
          seen.current = result;
          refresh();
        }
      }),
    [refresh],
  );
}

export function ScenesView() {
  const scenes = useScenesList();
  const trigger = useTriggerScene();
  useRefreshOnCompletion();

  if (scenes.isPending) {
    return (
      <div className="view" aria-busy="true" aria-label="Loading scenes">
        <h1 className="view-title">Scenes</h1>
        <div className="grid grid-cols-2 desktop:grid-cols-3 gap-4">
          <Skeleton className="h-touch-primary w-full" />
          <Skeleton className="h-touch-primary w-full" />
          <Skeleton className="h-touch-primary w-full" />
        </div>
      </div>
    );
  }

  if (scenes.isError) {
    return (
      <div className="view">
        <h1 className="view-title">Scenes</h1>
        <ErrorState
          title="Could not load scenes"
          detail="The controller did not answer. Nothing has been changed."
          status={statusLine(scenes.error)}
          onRetry={() => void scenes.refetch()}
        />
      </div>
    );
  }

  const list: readonly Scene[] = [...(scenes.data?.scenes ?? [])].sort((a, b) => a.sort_order - b.sort_order);

  if (list.length === 0) {
    return (
      <div className="view">
        <h1 className="view-title">Scenes</h1>
        <EmptyState icon={Sparkles} title="No scenes configured" detail="Scenes appear here once an admin creates one." />
      </div>
    );
  }

  function onTrigger(scene: Scene) {
    trigger.mutate(scene.id, { onError: (error) => void presentError(error) });
  }

  return (
    <div className="view">
      <h1 className="view-title">Scenes</h1>
      <div className="grid grid-cols-2 desktop:grid-cols-3 gap-4">
        {list.map((scene) => (
          <SceneCard key={scene.id} scene={scene} onTrigger={onTrigger} triggering={trigger.isPending && trigger.variables === scene.id} />
        ))}
      </div>
    </div>
  );
}
