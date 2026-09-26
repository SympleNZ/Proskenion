/*
 * A small renderer for the Markdown subset the four bundled run sheets and
 * hardware docs actually use (spec §21.24 *Help*, Simon's 26 Sep decision to
 * serve documentation from the controller itself): headings, paragraphs,
 * bullet and numbered lists, checkboxes, bold, italic, inline code, links,
 * tables and blockquotes. No fenced code block appears in any of the four
 * today, but one is supported anyway since a doc added later might use one.
 *
 * Written by hand rather than pulling in a Markdown library: the brief asks
 * for no new *runtime* dependency, and a full CommonMark parser is a great
 * deal of machinery for four short, hand-written documents whose syntax is
 * this bounded — the alternative, pre-rendering at build time, would still
 * need a Markdown dependency (a dev one) and a Vite plugin, for content this
 * small. `markdown.test.tsx` renders each of the four real files through
 * this and checks nothing throws, alongside targeted cases per construct.
 *
 * A link `[text](target)` navigates *inside the app* when `target` names
 * another bundled doc (`docs.ts`'s `DOCS`, matched by filename with any
 * `./` prefix or `#fragment` stripped) — none of the four currently link to
 * each other this way, they mention one another in inline code instead, but
 * the renderer supports it so a future edit to the docs (which this task
 * does not touch) works without a code change. An `http(s)://` link opens
 * normally; anything else — a relative path to a doc that is not bundled,
 * such as `docs/hardware/recovery.md` — has nowhere to go from an offline
 * appliance with no server-side file browser, so it renders as plain text
 * rather than a dead link.
 */
import { Fragment, type ReactNode } from "react";

import type { DocId } from "./docs";
import { DOCS } from "./docs";

function docIdForTarget(target: string): DocId | null {
  const stripped = target.replace(/^\.?\//, "").replace(/#.*$/, "");
  const doc = DOCS.find((d) => d.filename === stripped || `${d.id}.md` === stripped);
  return doc?.id ?? null;
}

interface InlineProps {
  text: string;
  onNavigate: (id: DocId) => void;
}

/** The earliest of code/link/bold/italic in `text`, or `null` if none appear. */
function firstInlineMatch(text: string): { index: number; length: number; kind: "code" | "link" | "bold" | "italic"; match: RegExpExecArray } | null {
  const candidates: { re: RegExp; kind: "code" | "link" | "bold" | "italic" }[] = [
    { re: /`([^`]+)`/, kind: "code" },
    { re: /\[([^\]]+)\]\(([^)]+)\)/, kind: "link" },
    { re: /\*\*([^*]+)\*\*/, kind: "bold" },
    { re: /\*([^*]+)\*/, kind: "italic" },
  ];
  let best: { index: number; length: number; kind: "code" | "link" | "bold" | "italic"; match: RegExpExecArray } | null = null;
  for (const { re, kind } of candidates) {
    const match = re.exec(text);
    if (match && (best === null || match.index < best.index)) {
      best = { index: match.index, length: match[0].length, kind, match };
    }
  }
  return best;
}

/** Renders inline formatting within one block's text — a heading, a paragraph, a list item, a table cell. */
function Inline({ text, onNavigate }: InlineProps): ReactNode {
  const found = firstInlineMatch(text);
  if (!found) return text;

  const before = text.slice(0, found.index);
  const after = text.slice(found.index + found.length);
  const key = `${found.kind}-${found.index}`;

  let node: ReactNode;
  switch (found.kind) {
    case "code":
      node = <code key={key}>{found.match[1]}</code>;
      break;
    case "bold":
      node = (
        <strong key={key}>
          <Inline text={found.match[1] ?? ""} onNavigate={onNavigate} />
        </strong>
      );
      break;
    case "italic":
      node = (
        <em key={key}>
          <Inline text={found.match[1] ?? ""} onNavigate={onNavigate} />
        </em>
      );
      break;
    case "link": {
      const linkText = found.match[1] ?? "";
      const target = found.match[2] ?? "";
      const docId = docIdForTarget(target);
      if (docId) {
        node = (
          <button key={key} type="button" className="link-button" onClick={() => onNavigate(docId)}>
            {linkText}
          </button>
        );
      } else if (/^https?:\/\//.test(target)) {
        node = (
          <a key={key} href={target} target="_blank" rel="noopener noreferrer">
            {linkText}
          </a>
        );
      } else {
        node = <Fragment key={key}>{linkText}</Fragment>;
      }
      break;
    }
  }

  return (
    <>
      {before}
      {node}
      <Inline text={after} onNavigate={onNavigate} />
    </>
  );
}

type Block =
  | { type: "heading"; level: number; text: string }
  | { type: "paragraph"; text: string }
  | { type: "blockquote"; text: string }
  | { type: "hr" }
  | { type: "code"; text: string }
  | { type: "list"; ordered: boolean; items: { checked: boolean | null; text: string }[] }
  | { type: "table"; header: string[]; rows: string[][] };

const HEADING_RE = /^(#{1,6})\s+(.*)$/;
const HR_RE = /^-{3,}$/;
const QUOTE_RE = /^>\s?(.*)$/;
const BULLET_RE = /^[-*]\s+(.*)$/;
const ORDERED_RE = /^\d+\.\s+(.*)$/;
const CHECKBOX_RE = /^\[([ xX])\]\s+(.*)$/;
const FENCE_RE = /^```/;
const TABLE_SEP_RE = /^\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?$/;

function splitRow(line: string): string[] {
  return line
    .trim()
    .replace(/^\|/, "")
    .replace(/\|$/, "")
    .split("|")
    .map((cell) => cell.trim());
}

function parseBlocks(source: string): Block[] {
  const lines = source.replace(/\r\n/g, "\n").split("\n");
  const blocks: Block[] = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i] ?? "";

    if (line.trim() === "") {
      i++;
      continue;
    }

    if (FENCE_RE.test(line)) {
      const codeLines: string[] = [];
      i++;
      while (i < lines.length && !FENCE_RE.test(lines[i] ?? "")) {
        codeLines.push(lines[i] ?? "");
        i++;
      }
      i++; // the closing fence
      blocks.push({ type: "code", text: codeLines.join("\n") });
      continue;
    }

    const heading = HEADING_RE.exec(line);
    if (heading) {
      blocks.push({ type: "heading", level: (heading[1] ?? "#").length, text: heading[2] ?? "" });
      i++;
      continue;
    }

    if (HR_RE.test(line.trim())) {
      blocks.push({ type: "hr" });
      i++;
      continue;
    }

    const quote = QUOTE_RE.exec(line);
    if (quote) {
      const quoteLines = [quote[1] ?? ""];
      i++;
      while (i < lines.length) {
        const next = QUOTE_RE.exec(lines[i] ?? "");
        if (!next) break;
        quoteLines.push(next[1] ?? "");
        i++;
      }
      blocks.push({ type: "blockquote", text: quoteLines.join(" ").trim() });
      continue;
    }

    // A table: a row containing "|", immediately followed by a separator row.
    if (line.includes("|") && TABLE_SEP_RE.test((lines[i + 1] ?? "").trim())) {
      const header = splitRow(line);
      i += 2;
      const rows: string[][] = [];
      while (i < lines.length && (lines[i] ?? "").includes("|") && (lines[i] ?? "").trim() !== "") {
        rows.push(splitRow(lines[i] ?? ""));
        i++;
      }
      blocks.push({ type: "table", header, rows });
      continue;
    }

    const bullet = BULLET_RE.exec(line);
    const ordered = ORDERED_RE.exec(line);
    if (bullet || ordered) {
      const isOrdered = ordered !== null;
      const itemRe = isOrdered ? ORDERED_RE : BULLET_RE;
      const items: { checked: boolean | null; text: string }[] = [];
      while (i < lines.length) {
        const m = itemRe.exec(lines[i] ?? "");
        if (!m) break;
        const raw = m[1] ?? "";
        const checkbox = CHECKBOX_RE.exec(raw);
        items.push(checkbox ? { checked: (checkbox[1] ?? " ").toLowerCase() === "x", text: checkbox[2] ?? "" } : { checked: null, text: raw });
        i++;
      }
      blocks.push({ type: "list", ordered: isOrdered, items });
      continue;
    }

    // A paragraph: consecutive plain lines up to the next blank line or block marker.
    const paragraphLines = [line];
    i++;
    while (i < lines.length) {
      const next = lines[i] ?? "";
      if (
        next.trim() === "" ||
        HEADING_RE.test(next) ||
        HR_RE.test(next.trim()) ||
        QUOTE_RE.test(next) ||
        BULLET_RE.test(next) ||
        ORDERED_RE.test(next) ||
        FENCE_RE.test(next) ||
        (next.includes("|") && TABLE_SEP_RE.test((lines[i + 1] ?? "").trim()))
      ) {
        break;
      }
      paragraphLines.push(next);
      i++;
    }
    blocks.push({ type: "paragraph", text: paragraphLines.join(" ").trim() });
  }

  return blocks;
}

function ListItem({ item, onNavigate }: { item: { checked: boolean | null; text: string }; onNavigate: (id: DocId) => void }) {
  if (item.checked === null) {
    return (
      <li>
        <Inline text={item.text} onNavigate={onNavigate} />
      </li>
    );
  }
  return (
    <li className="doc-checkbox-item">
      <span aria-hidden="true">{item.checked ? "☑" : "☐"}</span>
      <span className="sr-only">{item.checked ? "Checked: " : "Unchecked: "}</span>
      <Inline text={item.text} onNavigate={onNavigate} />
    </li>
  );
}

export interface RenderedMarkdownProps {
  source: string;
  onNavigate: (id: DocId) => void;
}

/** Renders one bundled document's Markdown as the doc content (spec §21.24). */
export function RenderedMarkdown({ source, onNavigate }: RenderedMarkdownProps) {
  const blocks = parseBlocks(source);
  return (
    <div className="doc-content">
      {blocks.map((block, index) => {
        switch (block.type) {
          case "heading": {
            const level = Math.min(Math.max(block.level, 1), 6);
            const Heading = (`h${level}` as const) as "h1" | "h2" | "h3" | "h4" | "h5" | "h6";
            return (
              <Heading key={index} className="sect-label">
                <Inline text={block.text} onNavigate={onNavigate} />
              </Heading>
            );
          }
          case "paragraph":
            return (
              <p key={index}>
                <Inline text={block.text} onNavigate={onNavigate} />
              </p>
            );
          case "blockquote":
            return (
              <blockquote key={index}>
                <Inline text={block.text} onNavigate={onNavigate} />
              </blockquote>
            );
          case "hr":
            return <hr key={index} />;
          case "code":
            return (
              <pre key={index}>
                <code>{block.text}</code>
              </pre>
            );
          case "list": {
            const List = block.ordered ? "ol" : "ul";
            return (
              <List key={index}>
                {block.items.map((item, itemIndex) => (
                  <ListItem key={itemIndex} item={item} onNavigate={onNavigate} />
                ))}
              </List>
            );
          }
          case "table":
            return (
              <div key={index} className="table-scroll">
                <table className="data-table">
                  <thead>
                    <tr>
                      {block.header.map((cell, cellIndex) => (
                        <th key={cellIndex} scope="col">
                          <Inline text={cell} onNavigate={onNavigate} />
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {block.rows.map((row, rowIndex) => (
                      <tr key={rowIndex}>
                        {row.map((cell, cellIndex) => (
                          <td key={cellIndex}>
                            <Inline text={cell} onNavigate={onNavigate} />
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            );
        }
      })}
    </div>
  );
}
