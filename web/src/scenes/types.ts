/*
 * Scene types the operator surface needs (spec §8.11, §16.5, §21.10).
 *
 * `priority` and `result` mirror the backend's closed vocabularies exactly —
 * §8.14 for priority, §8.15/§8.16 for a run's result — and are never widened
 * or re-derived here. The admin editor's richer types (actions, domains,
 * per-run reports) live in `@/admin/scenes/types`; this file is only what a
 * scene card needs.
 */

export type ScenePriority = "normal" | "critical";

/** §8.16's `SceneExecutionLog.result`. A scene with no actions is "success". */
export type SceneRunOutcome = "success" | "partial" | "failed";

export interface SceneLastRun {
  log_id: number;
  triggered_by: string;
  started_at: string;
  completed_at: string | null;
  result: SceneRunOutcome | null;
}

export interface Scene {
  id: number;
  name: string;
  description: string | null;
  enabled: boolean;
  icon: string | null;
  priority: ScenePriority;
  protected: boolean;
  visible_operator: boolean;
  sort_order: number;
  created_at: string;
  updated_at: string;
  /** From the engine, live — §16.5's `GET /scenes`. The socket confirms it sooner. */
  running: boolean;
  last_run: SceneLastRun | null;
}

export interface ScenesResponse {
  scenes: readonly Scene[];
}

export interface TriggerResponse {
  run_id: number;
  scene_id: number;
  priority: ScenePriority;
  triggered_by: string;
  started_at: string;
}
