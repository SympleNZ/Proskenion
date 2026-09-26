/*
 * Stage plan shapes (spec §9.3): `GET /lighting/bars` and
 * `GET /lighting/patch/conflicts` return exactly this shape; this file is
 * what the frontend and its tests share.
 * `LightingChannel` (bar_id, position, visible_staff) already lives in
 * `@/lighting/types` and is reused rather than duplicated.
 */

export type StagePlanMode = "admin" | "operator" | "hirer";

export interface LightingBar {
  id: number;
  name: string;
  /** 0 is downstage — the proscenium — ascending upstage (§9.3). */
  sort_order: number;
  notes: string | null;
  updated_at: string;
}

export interface LightingBarsResponse {
  bars: readonly LightingBar[];
}

/** Fixtures whose DMX addresses overlap (§9.1). Overlap warns; it never blocks. */
export interface PatchConflict {
  channel_ids: readonly number[];
  device_id: number | string;
  universe: number;
  slots: readonly number[];
}

export interface PatchConflictsResponse {
  conflicts: readonly PatchConflict[];
}
