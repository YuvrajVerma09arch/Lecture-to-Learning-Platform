/**
 * The notes document.
 *
 * The Markdown that the backend produced is rendered with a few
 * replacements that the document's own shape calls for: the `Source:
 * 0:44 - 3:56` line becomes a button that shows the transcript behind
 * it, the blockquote that starts with "Needs the original visual
 * material." becomes a labelled note instead of an ordinary quote, and
 * the short code spans that hold equations are rendered with KaTeX.
 */
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

function hastText(node?: HastNode): string {
  if (!node) return "";
  if (node.value) return node.value;
  return (node.children ?? []).map(hastText).join("");
}

function headingId(node?: HastNode): string {
  const id = node?.properties?.id;
  return typeof id === "string" ? id : "";
}

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
  const components = {
    h1: ({ node, children }: { node?: HastNode; children?: React.ReactNode }) => (
      <h1
        id={headingId(node)}
        tabIndex={-1}
        className="mt-2 text-2xl font-bold tracking-tight text-ink outline-none"
      >
        {children}
      </h1>
    ),
    h2: ({ node, children }: { node?: HastNode; children?: React.ReactNode }) => (
      <h2
        id={headingId(node)}
        tabIndex={-1}
        className="mt-8 scroll-mt-24 text-xl font-semibold tracking-tight text-ink outline-none"
      >
        {children}
      </h2>
    ),
    h3: ({ node, children }: { node?: HastNode; children?: React.ReactNode }) => (
      <h3
        id={headingId(node)}
        tabIndex={-1}
        className="mt-6 scroll-mt-24 text-base font-semibold text-ink outline-none"
      >
        {children}
      </h3>
    ),
    p: ({ node, children }: { node?: HastNode; children?: React.ReactNode }) => {
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
            <SourceExcerpt
              start={span.start}
              end={span.end}
              segments={segments}
            />
          ) : null}
        </div>
      );
    },
    blockquote: ({
      node,
      children,
    }: {
      node?: HastNode;
      children?: React.ReactNode;
    }) => {
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
    },
    code: ({
      className,
      children,
    }: {
      className?: string;
      children?: React.ReactNode;
    }) => {
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
    },
    pre: ({ children }: { children?: React.ReactNode }) => (
      <pre className="mt-4 overflow-x-auto rounded-md border border-line bg-canvas p-3 text-sm">
        {children}
      </pre>
    ),
    a: ({
      href,
      children,
    }: {
      href?: string;
      children?: React.ReactNode;
    }) => (
      <a
        href={href}
        target="_blank"
        rel="noreferrer noopener"
        className="text-accent-ink underline underline-offset-2"
      >
        {children}
      </a>
    ),
    table: ({ children }: { children?: React.ReactNode }) => (
      <div className="mt-4 overflow-x-auto">
        <table className="w-full border-collapse text-sm">{children}</table>
      </div>
    ),
    th: ({ children }: { children?: React.ReactNode }) => (
      <th className="border border-line bg-canvas px-2 py-1 text-left font-semibold text-ink">
        {children}
      </th>
    ),
    td: ({ children }: { children?: React.ReactNode }) => (
      <td className="border border-line px-2 py-1 text-body">{children}</td>
    ),
  } as unknown as Components;

  return (
    <div className="notes-body">
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath, remarkHeadingIds]}
        rehypePlugins={[rehypeKatex]}
        components={components}
      >
        {markdown}
      </ReactMarkdown>
    </div>
  );
}
