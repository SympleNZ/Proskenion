/*
 * One scene card (spec §21.10). Five states — idle, executing, success,
 * partial, failed — driven by the live store (§16.8, §21.2), never
 * re-derived: `state.scenes.running` says whether it is executing right now,
 * and the most recent `scene_completed` (or a `scenes_state` resync) says how
 * the last run went. The brief bloom on success/partial/failed settles back
 * to idle after `--duration-very-slow`, the token the design reserves for
 * "scene result reveal".
 */
import { Lock, Sparkles } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { getScenes, useScenes, type SceneResult } from "@/live/store";
import { formatRelative } from "@/lib/time";

import type { Scene } from "./types";

export type SceneCardState = "idle" | "executing" | "success" | "partial" | "failed";

const BLOOM_MS = 600; // --duration-very-slow (§21.3): "Scene result reveal"

function outcomeState(outcome: SceneResult["result"]): "success" | "partial" | "failed" {
  return outcome === "partial" || outcome === "failed" ? outcome : "success";
}

/** Executing beats a settling bloom; a stale bloom for another scene is ignored. */
function useCardState(sceneId: number): SceneCardState {
  const scenes = useScenes();
  const running = scenes.running.has(sceneId);
  const [bloom, setBloom] = useState<"success" | "partial" | "failed" | null>(null);
  const seen = useRef<SceneResult | null>(null);

  useEffect(() => {
    const result = getScenes().lastResult;
    if (!result || result.sceneId !== sceneId || result === seen.current) return undefined;
    seen.current = result;
    setBloom(outcomeState(result.result));
    const timer = window.setTimeout(() => setBloom(null), BLOOM_MS);
    return () => window.clearTimeout(timer);
  }, [scenes.lastResult, sceneId]);

  if (running) return "executing";
  return bloom ?? "idle";
}

export interface SceneCardProps {
  scene: Scene;
  onTrigger: (scene: Scene) => void;
  triggering: boolean;
}

export function SceneCard({ scene, onTrigger, triggering }: SceneCardProps) {
  const state = useCardState(scene.id);
  const disabled = !scene.enabled || triggering;
  const reason = !scene.enabled ? "This scene is disabled and cannot be triggered" : undefined;
  const metaLine = reason ?? (scene.last_run ? `Last ran ${formatRelative(scene.last_run.completed_at ?? scene.last_run.started_at)}` : "Never run");

  return (
    <button
      type="button"
      className="scene-card"
      data-state={state}
      data-critical={scene.priority === "critical" ? "true" : undefined}
      disabled={disabled}
      aria-disabled={disabled || undefined}
      title={reason}
      onClick={() => onTrigger(scene)}
    >
      {scene.icon ? (
        <span className="scene-card-icon" aria-hidden="true">
          {scene.icon}
        </span>
      ) : (
        <Sparkles aria-hidden="true" className="scene-card-icon size-6" />
      )}
      <span className="scene-card-name">{scene.name}</span>
      {scene.protected ? (
        <span className="scene-card-badge">
          <Lock aria-hidden="true" className="size-3" />
          Protected
        </span>
      ) : null}
      <span className="scene-card-meta">{metaLine}</span>
    </button>
  );
}
