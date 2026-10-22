/**
 * The notes document.
 *
 * The Markdown that the backend produced is rendered with a few
 * replacements that the document's own shape calls for: the `Source:
 * 0:44 - 3:56` line becomes a button that shows the transcript behind
 * it, the blockquote that starts with "Needs the original visual
 * material." becomes a labelled note instead of an ordinary quote, and
 * the short code spans that hold equations are rendered with KaTeX.
 *
 * The renderers are defined once, outside the component, and read the
 * document's state from a context. Rebuilding them on every render
 * would remount the whole document and lose the reader's place.
 */
import { createContext, useContext, type ReactNode } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import {
  UNCERTAIN_MARKER,
  looksLikeEquation,
  parseSourceSpan,
  renderMath,
  remarkHeadingIds,
  type SourceSpan,
} from "../lib/markdown";
import type { Segment } from "../types";
import { SourceExcerpt } from "./SourceExcerpt";

interface HastNode {
  type: string;
  tagName?: string;
  value?: string;
  properties?: Record<string, unknown>;
  children?: HastNode[];
}

interface NotesContextValue {
  activeSpan: SourceSpan | null;
  onToggleSource: (span: SourceSpan) => void;
  segments: Segment[];
}

const NotesContext = createContext<NotesContextValue>({
  activeSpan: null,
  onToggleSource: () => undefined,
  segments: [],
});

function hastText(node?: HastNode): string {
  if (!node) return "";
  if (node.value) return node.value;
  return (node.children ?? []).map(hastText).join("");
}

function headingId(node?: HastNode): string {
  const id = node?.properties?.id;
  return typeof id === "string" ? id : "";
}

function Heading1({ node, children }: { node?: HastNode; children?: ReactNode }) {
  return (
    <h1
      id={headingId(node)}
      tabIndex={-1}
      className="mt-2 text-2xl font-bold tracking-tight text-ink outline-none"
    >
      {children}
    </h1>
  );
}

function Heading2({ node, children }: { node?: HastNode; children?: ReactNode }) {
  return (
    <h2
      id={headingId(node)}
      tabIndex={-1}
      className="mt-8 scroll-mt-24 text-xl font-semibold tracking-tight text-ink outline-none"
    >
      {children}
    </h2>
  );
}

function Heading3({ node, children }: { node?: HastNode; children?: ReactNode }) {
  return (
    <h3
      id={headingId(node)}
      tabIndex={-1}
      className="mt-6 scroll-mt-24 text-base font-semibold text-ink outline-none"
    >
      {children}
    </h3>
  );
}

function Paragraph({ node, children }: { node?: HastNode; children?: ReactNode }) {
  const { activeSpan, onToggleSource, segments } = useContext(NotesContext);
  const span = parseSourceSpan(hastText(node));
  if (!span) return <p className="mt-4 leading-7 text-body">{children}</p>;

  const active =
    activeSpan !== null &&
    activeSpan.start === span.start &&
    activeSpan.end === span.end;

  return (
    <div className="mt-3">
      <button
        type="button"
        data-source-start={span.start}
        onClick={() => onToggleSource(span)}
        aria-expanded={active}
        className="inline-flex items-center gap-2 rounded-md border border-strong-line bg-canvas px-2.5 py-1.5 text-sm font-medium text-accent-ink hover:bg-white"
      >
        <span aria-hidden="true">{active ? "▾" : "▸"}</span>
        <span>
          Source: {span.label}
          <span className="ml-2 font-normal text-muted">
            {active ? "hide transcript" : "show transcript"}
          </span>
        </span>
      </button>
      {active ? (
        <SourceExcerpt start={span.start} end={span.end} segments={segments} />
      ) : null}
    </div>
  );
}

function Blockquote({
  node,
  children,
}: {
  node?: HastNode;
  children?: ReactNode;
}) {
  if (hastText(node).trim().startsWith(UNCERTAIN_MARKER)) {
    return (
      <aside
        aria-label="Visual material not in the audio"
        className="mt-4 rounded-md border border-strong-line border-l-4 bg-canvas px-3 py-2"
      >
        <p className="text-xs font-semibold uppercase tracking-wide text-ink">
          Visual material not in the audio
        </p>
        <div className="mt-1 text-sm text-body">{children}</div>
      </aside>
    );
  }
  return (
    <blockquote className="mt-4 border-l-2 border-strong-line pl-3 italic text-muted">
      {children}
    </blockquote>
  );
}

function Code({
  className,
  children,
}: {
  className?: string;
  children?: ReactNode;
}) {
  const text = String(children ?? "");
  if (className?.startsWith("language-")) {
    return <code className={className}>{children}</code>;
  }
  if (looksLikeEquation(text)) {
    const html = renderMath(text);
    if (html) {
      return (
        <span
          className="math-inline"
          dangerouslySetInnerHTML={{ __html: html }}
        />
      );
    }
  }
  return (
    <code className="rounded bg-canvas px-1 py-0.5 font-mono text-[0.9em] text-ink">
      {children}
    </code>
  );
}

const COMPONENTS = {
  h1: Heading1,
  h2: Heading2,
  h3: Heading3,
  p: Paragraph,
  blockquote: Blockquote,
  code: Code,
  pre: ({ children }: { children?: ReactNode }) => (
    <pre className="mt-4 overflow-x-auto rounded-md border border-line bg-canvas p-3 text-sm">
      {children}
    </pre>
  ),
  a: ({ href, children }: { href?: string; children?: ReactNode }) => (
    <a
      href={href}
      target="_blank"
      rel="noreferrer noopener"
      className="text-accent-ink underline underline-offset-2"
    >
      {children}
    </a>
  ),
  table: ({ children }: { children?: ReactNode }) => (
    <div className="mt-4 overflow-x-auto">
      <table className="w-full border-collapse text-sm">{children}</table>
    </div>
  ),
  th: ({ children }: { children?: ReactNode }) => (
    <th className="border border-line bg-canvas px-2 py-1 text-left font-semibold text-ink">
      {children}
    </th>
  ),
  td: ({ children }: { children?: ReactNode }) => (
    <td className="border border-line px-2 py-1 text-body">{children}</td>
  ),
} as unknown as Components;

export function NotesDocument({
  markdown,
  segments,
  activeSpan,
  onToggleSource,
}: {
  markdown: string;
  segments: Segment[];
  activeSpan: SourceSpan | null;
  onToggleSource: (span: SourceSpan) => void;
}) {
  const context: NotesContextValue = { activeSpan, onToggleSource, segments };

  return (
    <div className="notes-body">
      <NotesContext.Provider value={context}>
        <ReactMarkdown
          remarkPlugins={[remarkGfm, remarkMath, remarkHeadingIds]}
          rehypePlugins={[rehypeKatex]}
          components={COMPONENTS}
        >
          {markdown}
        </ReactMarkdown>
      </NotesContext.Provider>
    </div>
  );
}
