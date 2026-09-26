/*
 * Client-side peeking at an upload before it is sent (§21.19 *Import wizard*
 * step 2). `POST /knx/import` refuses a generic CSV/TSV outright when it is
 * not given a column mapping (`knx_import.parse_generic` requires one), so
 * the wizard cannot ask the server for the file's columns the way it can for
 * ETS CSV or XML — it has to read the header row itself first, to know
 * whether to show the mapping step at all. This mirrors
 * `proskenion/core/knx_import.py`'s own sniffing (`sniff_format`,
 * `_looks_like_xml`, `_sniff_delimiter`) closely enough that this module's
 * guess and the server's own detection agree in the overwhelming common
 * case; where they would not (an edge case in delimiter sniffing on a
 * malformed file), the server's `format` parameter this wizard always sends
 * once it has decided is what actually governs parsing — this module only
 * decides which step 2 UI to show.
 */

function looksLikeXml(bytes: Uint8Array): boolean {
  let index = 0;
  while (index < bytes.length) {
    const byte = bytes[index];
    // UTF-8 BOM, space, tab, CR, LF
    if (byte === 0xef || byte === 0xbb || byte === 0xbf || byte === 0x20 || byte === 0x09 || byte === 0x0d || byte === 0x0a) {
      index += 1;
      continue;
    }
    break;
  }
  return bytes[index] === 0x3c; // "<"
}

function sniffDelimiter(firstLine: string): string {
  const tabs = (firstLine.match(/\t/g) ?? []).length;
  const commas = (firstLine.match(/,/g) ?? []).length;
  return tabs > 0 && tabs >= commas ? "\t" : ",";
}

export interface FilePeek {
  looksXml: boolean;
  /** Empty for XML — a header row is meaningless there. */
  header: string[];
  /** The first data row, aligned with `header`, for the mapping step's
   * "Sample" column (§21.19 step 2). Empty when the file has no second line. */
  sample: string[];
}

/** Reads only as much of the file as JavaScript's `File`/`FileReader` give
 * cheap access to — the whole thing, but locally, with no network round trip. */
export async function peekFile(file: File): Promise<FilePeek> {
  const buffer = await file.arrayBuffer();
  const bytes = new Uint8Array(buffer);
  if (looksLikeXml(bytes)) return { looksXml: true, header: [], sample: [] };
  const text = new TextDecoder("utf-8").decode(bytes);
  const lines = text.split(/\r\n|\r|\n/);
  const firstLine = lines[0] ?? "";
  const delimiter = sniffDelimiter(firstLine);
  const header = firstLine.split(delimiter).map((cell) => cell.trim());
  const sample = (lines[1] ?? "").split(delimiter).map((cell) => cell.trim());
  return { looksXml: false, header, sample };
}

/** Mirrors `sniff_format`'s ETS-CSV test: does the header carry Main, Middle
 * and Sub (any case)? If so, no mapping step is needed — §7.1's fixed ETS
 * column layout applies and the server fills it in automatically. */
export function looksLikeEtsCsv(header: readonly string[]): boolean {
  const lower = new Set(header.map((cell) => cell.toLowerCase()));
  return lower.has("main") && lower.has("middle") && lower.has("sub");
}
