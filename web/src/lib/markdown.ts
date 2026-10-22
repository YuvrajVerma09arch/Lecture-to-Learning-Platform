/**
 * Markdown helpers for the notes reader.
 *
 * The notes document is produced by the backend, so its shape is known:
 * `##` section headings, a `*Source: 0:44 - 3:56*` line under each
 * section, a blockquote that starts with "Needs the original visual
 * material." for material that was shown but not spoken, and equations
 * written as inline code.
 */
import { unified } from "unified";
import remarkParse from "remark-parse";
import katex from "katex";
import { parseTime, slugify } from "./format";

export interface MdNode {
  type: string;
  depth?: number;
  value?: string;
  children?: MdNode[];
  data?: { hProperties?: Record<string, unknown> };
}

export interface Heading {
  text: string;
  id: string;
  level: number;
}

export interface SourceSpan {
  start: number;
  end: number;
  label: string;
}

/** The phrase the backend uses when a section depends on unseen material. */
export const UNCERTAIN_MARKER = "Needs the original visual material.";

/** A `*Source: 0:44 - 3:56*` line. */
const SOURCE_PATTERN =
  /^\s*Source:\s*(\d{1,2}:\d{2}(?::\d{2})?)\s*[–—-]\s*(\d{1,2}:\d{2}(?::\d{2})?)\s*$/;

function walk(node: MdNode, visit: (node: MdNode) => void): void {
  visit(node);
  node.children?.forEach((child) => walk(child, visit));
}

function nodeText(node: MdNode): string {
  if (node.value) return node.value;
  return (node.children ?? []).map(nodeText).join("");
}

/**
 * Give every heading a stable id. The same function is used for the
 * table of contents and for the rendered document, so the two always
 * agree.
 */
export function assignHeadingIds(tree: MdNode): void {
  const seen = new Map<string, number>();
  walk(tree, (node) => {
    if (node.type !== "heading") return;
    const base = slugify(nodeText(node));
    const count = seen.get(base) ?? 0;
    seen.set(base, count + 1);
    const id = count === 0 ? base : `${base}-${count}`;
    node.data = node.data ?? {};
    node.data.hProperties = { ...(node.data.hProperties ?? {}), id };
  });
}

/** A remark plugin that adds heading ids to the document react-markdown renders. */
export function remarkHeadingIds() {
  return (tree: unknown): void => {
    assignHeadingIds(tree as MdNode);
  };
}

/** The headings of a document, in order, with the ids the renderer will use. */
export function extractHeadings(markdown: string): Heading[] {
  const tree = unified().use(remarkParse).parse(markdown) as unknown as MdNode;
  assignHeadingIds(tree);
  const headings: Heading[] = [];
  walk(tree, (node) => {
    if (node.type !== "heading") return;
    const id = node.data?.hProperties?.id;
    headings.push({
      text: nodeText(node),
      id: typeof id === "string" ? id : slugify(nodeText(node)),
      level: node.depth ?? 1,
    });
  });
  return headings;
}

/** Parse a `Source: 0:44 - 3:56` line into seconds. */
export function parseSourceSpan(text: string): SourceSpan | null {
  const match = SOURCE_PATTERN.exec(text);
  if (!match) return null;
  const start = parseTime(match[1]);
  const end = parseTime(match[2]);
  if (start === null || end === null) return null;
  return { start, end, label: `${match[1]} – ${match[2]}` };
}

/** Every `Source:` span in a document, in order. */
export function extractSourceSpans(markdown: string): SourceSpan[] {
  const spans: SourceSpan[] = [];
  const pattern = /Source:\s*\d{1,2}:\d{2}(?::\d{2})?\s*[–—-]\s*\d{1,2}:\d{2}(?::\d{2})?/g;
  for (const match of markdown.matchAll(pattern)) {
    const span = parseSourceSpan(match[0]);
    if (span) spans.push(span);
  }
  return spans;
}

/**
 * True for the short code spans the notes use for equations, such as
 * `F = ma` or `E = mc^2`.
 */
export function looksLikeEquation(text: string): boolean {
  if (!text || text.length > 160) return false;
  if (!/[=^≈→×÷±≤≥]/.test(text)) return false;
  return /^[A-Za-z0-9_\\{}^()[\] \t+\-*/.,:;'"|]+$/.test(text);
}

/** Render an expression with KaTeX, falling back to plain text on error. */
export function renderMath(expression: string): string | null {
  try {
    return katex.renderToString(expression, {
      throwOnError: false,
      displayMode: false,
      strict: false,
    });
  } catch {
    return null;
  }
}
