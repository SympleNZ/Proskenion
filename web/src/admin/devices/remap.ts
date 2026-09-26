/*
 * The re-mapping screen's rules, apart from its rendering (spec §5.5, §15.6).
 */
import type { ChannelRef, RemapChoice, RemapResponse, RemapRow } from "./types";

export function rowKey(row: Pick<RemapRow, "holder" | "id">): string {
  return `${row.holder}:${row.id}`;
}

/** A matrix input or output has no unmapped state (§15.10), so it must be mapped. */
export function isMatrix(row: RemapRow): boolean {
  return row.holder !== "mixer_channel";
}

/** What a row may point at: its own side of a matrix, and Main only for Main (§7.3). */
export function optionsFor(row: RemapRow, available: RemapResponse["available"]): ChannelRef[] {
  if (row.holder === "matrix_input") return available.inputs ?? [];
  if (row.holder === "matrix_output") return available.outputs ?? [];
  const refs = available.refs ?? [];
  return row.kind === "main" ? refs.filter((ref) => ref.kind === "main") : refs.filter((ref) => ref.kind !== "main");
}

/**
 * The body for `POST /devices/{id}/remap`. A mixer channel with any blank
 * picker is left unmapped as a whole: a reference with no equivalent leaves
 * the channel unresolved (§15.6).
 */
export function buildChoices(
  rows: readonly RemapRow[],
  picks: Readonly<Record<string, readonly string[]>>,
): RemapChoice[] {
  return rows.map((row) => {
    const chosen = picks[rowKey(row)] ?? [];
    const complete = chosen.length > 0 && chosen.every((ref) => ref !== "");
    return { holder: row.holder, id: row.id, new_refs: complete ? [...chosen] : null };
  });
}
