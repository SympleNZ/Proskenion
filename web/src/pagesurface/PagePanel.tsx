/*
 * A page's button panel (spec §21.9 "Buttons are square, and never scroll").
 * `row`/`col` are "a row-major reading order" (phase-5-contracts.md, Q5), not
 * a fixed pixel position: buttons are sorted into a flat sequence and the
 * grid lays that sequence out into however many columns `panelLayout`'s
 * joint search picks for the space actually available, exactly as
 * `docs/pages-device-sizes.html`'s own demo does with a plain ordered array.
 * Unfilled cells at the tail of the last row render dashed and empty.
 *
 * `surfaceWidth` comes from `PageSurface`, which measures the whole
 * horizontally-scrolling row once and hands every panel on it the same
 * number — a panel never measures its own width for this, because its own
 * width is `computePanelLayout`'s *output*, and using it as an input too
 * would feed the search its own answer back in.
 */
import type { CSSProperties } from "react";

import { useElementSize } from "@/lib/useElementSize";

import { BUTTON_FLOOR, HIRER_BUTTON_FLOOR, computePanelLayout } from "./panelLayout";
import { PanelButton } from "./PanelButton";
import type { PagePanelItem } from "./types";

/** Sized before the panel's own grid area has been measured — a reasonable guess for the first paint, corrected the instant `useElementSize`'s ref mounts. */
const DEFAULT_INNER_HEIGHT = 402; // 3 rows at the ideal button size, 2 row gaps
const DEFAULT_AVAILABLE_WIDTH = 960;

export interface PagePanelProps {
  pageId: number;
  item: PagePanelItem;
  /** The surface row's own visible width, in px — 0 before `PageSurface` has measured it. */
  surfaceWidth: number;
  /** §24.6's 72 px floor, not the operator's 64 px (§18) — the same joint search, run against a taller floor. */
  hirer?: boolean;
}

export function PagePanel({ pageId, item, surfaceWidth, hirer = false }: PagePanelProps) {
  const [gridRef, gridSize] = useElementSize<HTMLDivElement>();

  const ordered = [...item.buttons].sort((a, b) => a.row - b.row || a.col - b.col);
  const innerHeight = gridSize.height || DEFAULT_INNER_HEIGHT;
  const availableWidth = surfaceWidth || DEFAULT_AVAILABLE_WIDTH;
  const layout = computePanelLayout(ordered.length, item.panel_width, innerHeight, availableWidth, hirer ? HIRER_BUTTON_FLOOR : BUTTON_FLOOR);
  const totalCells = layout.cols * layout.rows;
  const cells = Array.from({ length: totalCells }, (_, index) => ordered[index] ?? null);

  const panelStyle = { width: layout.width } as CSSProperties;
  const gridStyle = {
    gridTemplateColumns: `repeat(${layout.cols}, ${layout.side}px)`,
    columnGap: layout.gap,
  } as CSSProperties;

  return (
    <div className="page-item page-panel" style={panelStyle} data-testid={`page-panel-${item.id}`}>
      <p className="panel-title">{item.panel_title}</p>
      <div ref={gridRef} className="panel-grid" style={gridStyle}>
        {cells.map((spec, index) =>
          spec ? (
            <PanelButton key={spec.id} pageId={pageId} spec={spec} side={layout.side} hirer={hirer} />
          ) : (
            <div key={`empty-${index}`} className="panel-empty-cell" aria-hidden="true" style={{ width: layout.side }} />
          ),
        )}
      </div>
    </div>
  );
}
