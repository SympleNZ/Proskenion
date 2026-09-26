/*
 * The admin Scenes screen (spec §21.16): the scene list, with create and
 * delete, and the entry point into the editor. A protected scene answers a
 * delete with 403 `permission_denied`, `detail.reason = "protected"` (§8.11)
 * — shown inline against the row as well as in the global toast, so it is
 * visible without depending on where the reader is looking.
 */
import { Lock, Sparkles } from "lucide-react";
import { useState } from "react";

import { ApiError } from "@/api/client";
import { presentError } from "@/api/errors";
import { Button } from "@/components/ui/Button";
import { Field, Input } from "@/components/ui/Input";
import { EmptyState, ErrorState, Skeleton } from "@/components/ui/EmptyState";
import { ConfirmDialog, Sheet, SheetContent } from "@/components/ui/Sheet";
import { saveFormOnShortcut } from "@/lib/keyboard";
import { useScenesList } from "@/scenes/api";
import type { Scene } from "@/scenes/types";

import { useCreateScene, useDeleteScene } from "./api";
import { LogViewer } from "./LogViewer";
import { SceneEditor } from "./SceneEditor";

function statusLine(error: unknown): string | undefined {
  return error instanceof ApiError ? `${error.status} ${error.code}` : undefined;
}

export function ScenesScreen() {
  const scenes = useScenesList();
  const createScene = useCreateScene();
  const deleteScene = useDeleteScene();
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");
  const [nameError, setNameError] = useState<string | undefined>();
  const [confirming, setConfirming] = useState<Scene | null>(null);
  const [refusal, setRefusal] = useState<{ id: number; message: string } | null>(null);
  const [logOpen, setLogOpen] = useState(false);

  if (selectedId !== null) {
    return <SceneEditor sceneId={selectedId} onBack={() => setSelectedId(null)} />;
  }

  if (scenes.isPending) {
    return (
      <div className="view" aria-busy="true" aria-label="Loading scenes">
        <h1 className="view-title">Scenes</h1>
        <Skeleton className="h-touch w-full" />
        <Skeleton className="h-touch w-full" />
      </div>
    );
  }

  if (scenes.isError) {
    return (
      <div className="view">
        <h1 className="view-title">Scenes</h1>
        <ErrorState title="Could not load scenes" status={statusLine(scenes.error)} onRetry={() => void scenes.refetch()} />
      </div>
    );
  }

  const list = [...(scenes.data?.scenes ?? [])].sort((a, b) => a.sort_order - b.sort_order);

  function submitCreate() {
    const name = newName.trim();
    if (!name) {
      setNameError("Give the scene a name");
      return;
    }
    createScene.mutate(
      { name },
      {
        onSuccess: (scene) => {
          setCreating(false);
          setNewName("");
          setNameError(undefined);
          setSelectedId(scene.id);
        },
        onError: (error) => void presentError(error),
      },
    );
  }

  function confirmDelete() {
    const scene = confirming;
    setConfirming(null);
    if (!scene) return;
    deleteScene.mutate(scene.id, {
      onError: (error) => {
        if (error instanceof ApiError && error.code === "permission_denied") {
          setRefusal({ id: scene.id, message: error.message });
        }
        presentError(error);
      },
    });
  }

  return (
    <div className="view">
      <header className="view-head">
        <div>
          <h1 className="view-title">Scenes</h1>
          <p className="view-lede">A scene is a set of actions across the eight domains, grouped by delay (§8.12).</p>
        </div>
        <div className="flex gap-3">
          <Button variant="secondary" onClick={() => setLogOpen(true)}>
            Execution log
          </Button>
          <Button variant="primary" helpId="scenes.new" onClick={() => setCreating(true)}>
            New scene
          </Button>
        </div>
      </header>

      {list.length === 0 ? (
        <EmptyState
          icon={Sparkles}
          title="No scenes yet"
          detail="A scene is what happens; a rule decides when (§8.1)."
          action={
            <Button variant="primary" helpId="scenes.new" onClick={() => setCreating(true)}>
              Create the first scene
            </Button>
          }
        />
      ) : (
        <ul className="scene-list">
          {list.map((scene) => (
            <li key={scene.id} className="scene-list-row">
              <button type="button" className="scene-list-name" onClick={() => setSelectedId(scene.id)}>
                <span aria-hidden="true">{scene.icon ?? "🎬"}</span>
                {scene.name}
                {scene.protected ? (
                  <span className="scene-card-badge">
                    <Lock aria-hidden="true" className="size-3" />
                    Protected
                  </span>
                ) : null}
                {!scene.enabled ? <span className="pill">Disabled</span> : null}
                {scene.priority === "critical" ? <span className="pill">Critical</span> : null}
              </button>
              <div className="flex gap-2">
                <Button variant="secondary" onClick={() => setSelectedId(scene.id)}>
                  Edit
                </Button>
                <Button variant="destructive" helpId="scenes.delete" onClick={() => setConfirming(scene)}>
                  Delete
                </Button>
              </div>
              {refusal?.id === scene.id ? <p className="text-danger-text text-sm">{refusal.message}</p> : null}
            </li>
          ))}
        </ul>
      )}

      <Sheet open={creating} onOpenChange={setCreating}>
        <SheetContent title="New scene" description="Give it a name — everything else can be set from the editor.">
          <form
            className="sheet-body"
            noValidate
            onKeyDown={saveFormOnShortcut}
            onSubmit={(event) => {
              event.preventDefault();
              submitCreate();
            }}
          >
            <Field label="Name" htmlFor="new-scene-name" helpId="scenes.new.name" error={nameError} errorId="new-scene-name-error">
              <Input id="new-scene-name" value={newName} onChange={(event) => setNewName(event.currentTarget.value)} />
            </Field>
            <div className="dialog-actions">
              <Button type="button" variant="secondary" onClick={() => setCreating(false)}>
                Cancel
              </Button>
              <Button type="submit" variant="primary" helpId="scenes.new.create" loading={createScene.isPending}>
                Create
              </Button>
            </div>
          </form>
        </SheetContent>
      </Sheet>

      <ConfirmDialog
        open={confirming !== null}
        onOpenChange={(open) => {
          if (!open) setConfirming(null);
        }}
        title={`Delete "${confirming?.name ?? ""}"?`}
        description="This removes the scene and every one of its actions. A scene referenced by a rule cannot be deleted (§15.8)."
        confirmLabel="Delete"
        destructive
        onConfirm={confirmDelete}
      />

      <LogViewer open={logOpen} onOpenChange={setLogOpen} />
    </div>
  );
}
