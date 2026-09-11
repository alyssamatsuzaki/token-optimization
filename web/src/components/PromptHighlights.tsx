/**
 * The lint's spans, drawn on the prompt the lint actually saw.
 *
 * The engine returns every finding with the exact character ranges it fired on
 * (`Span(section, block, start, end)`), and until now nothing drew them. The text here is the
 * text that was submitted, not the current contents of the textareas, because a span is only
 * meaningful against the string it was measured on.
 */
import { useMemo } from "react";

import type { FindingView } from "../lib/types";

/** How the API splits the system box into blocks (see `_inspect_request` in api/routes.py). */
const BLOCK_SEPARATOR = "\n\n---\n\n";

export interface InspectedPrompt {
  system: string;
  user: string;
}

interface Piece {
  text: string;
  ids: string[];
}

interface BlockView {
  key: string;
  label: string;
  pieces: Piece[];
  marked: number;
}

function promptBlocks(prompt: InspectedPrompt): { section: string; block: number; label: string; text: string }[] {
  const system = prompt.system.split(BLOCK_SEPARATOR).filter((chunk) => chunk.trim());
  const blocks = system.map((text, index) => ({
    section: "system",
    block: index,
    label: system.length > 1 ? `System block ${index + 1}` : "System prompt",
    text,
  }));
  if (prompt.user) blocks.push({ section: "user", block: 0, label: "User message", text: prompt.user });
  return blocks;
}

/** Split one block's text into plain and marked pieces, merging spans that overlap. */
function cut(text: string, spans: { start: number; end: number; id: string }[]): Piece[] {
  if (spans.length === 0) return [{ text, ids: [] }];
  const sorted = [...spans].sort((a, b) => a.start - b.start || a.end - b.end);
  const merged: { start: number; end: number; ids: string[] }[] = [];
  for (const span of sorted) {
    const start = Math.max(0, Math.min(span.start, text.length));
    const end = Math.max(start, Math.min(span.end, text.length));
    if (start === end) continue;
    const last = merged[merged.length - 1];
    if (last && start <= last.end) {
      last.end = Math.max(last.end, end);
      if (!last.ids.includes(span.id)) last.ids.push(span.id);
    } else {
      merged.push({ start, end, ids: [span.id] });
    }
  }
  const pieces: Piece[] = [];
  let cursor = 0;
  for (const range of merged) {
    if (range.start > cursor) pieces.push({ text: text.slice(cursor, range.start), ids: [] });
    pieces.push({ text: text.slice(range.start, range.end), ids: range.ids });
    cursor = range.end;
  }
  if (cursor < text.length) pieces.push({ text: text.slice(cursor), ids: [] });
  return pieces;
}

interface Props {
  prompt: InspectedPrompt;
  findings: FindingView[];
  /** When set, only this finding's spans are drawn. */
  selected: string | null;
}

/**
 * Read-only rendering of the inspected prompt with every lint span marked.
 *
 * Findings with no spans (whole-request rules like the output cap) cannot be drawn here; the
 * caller says so rather than leaving the reader to wonder why clicking one changes nothing.
 */
export default function PromptHighlights({ prompt, findings, selected }: Props) {
  const blocks = useMemo<BlockView[]>(() => {
    const shown = selected ? findings.filter((f) => f.id === selected) : findings;
    return promptBlocks(prompt).map((block) => {
      const spans = shown.flatMap((f) =>
        f.spans
          .filter((s) => s.section === block.section && s.block === block.block)
          .map((s) => ({ start: s.start, end: s.end, id: f.id })),
      );
      const pieces = cut(block.text, spans);
      return {
        key: `${block.section}-${block.block}`,
        label: block.label,
        pieces,
        marked: pieces.filter((p) => p.ids.length > 0).length,
      };
    });
  }, [prompt, findings, selected]);

  const total = blocks.reduce((sum, b) => sum + b.marked, 0);

  return (
    <div data-testid="prompt-highlights">
      <p className="text-small text-graphite mb-2">
        {total === 0
          ? selected
            ? `${selected} has no spans to draw: it is a finding about the request as a whole.`
            : "No finding named a range of this prompt."
          : `${total} highlighted range${total === 1 ? "" : "s"}${selected ? ` from ${selected}` : ""}. Click a finding above to isolate it.`}
      </p>
      {blocks.map((block) => (
        <div key={block.key} className="mb-3">
          <div className="text-micro text-graphite mb-1">
            {block.label}
            {block.marked > 0 && (
              <span className="ml-2 tabular-nums" data-testid={`highlight-count-${block.key}`}>
                {block.marked} marked
              </span>
            )}
          </div>
          <pre className="font-mono text-micro whitespace-pre-wrap border border-rule p-2 bg-chalk max-h-64 overflow-y-auto">
            {block.pieces.map((piece, index) =>
              piece.ids.length === 0 ? (
                <span key={index}>{piece.text}</span>
              ) : (
                <mark
                  key={index}
                  className="bg-vermilion/20 text-ink"
                  title={piece.ids.join(", ")}
                  data-testid="prompt-span"
                >
                  {piece.text}
                </mark>
              ),
            )}
          </pre>
        </div>
      ))}
    </div>
  );
}
