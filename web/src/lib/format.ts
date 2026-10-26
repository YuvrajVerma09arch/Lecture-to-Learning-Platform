/** Small shared helpers: time codes, slugs and plain-text extraction. */

import type { ReactNode } from "react";

/** `0:44` or `1:02:33`, matching the backend's `format_timestamp`. */
export function formatTime(seconds: number | null | undefined): string {
  const total = Math.max(0, Math.floor(seconds ?? 0));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  if (hours > 0) {
    return `${hours}:${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
  }
  return `${minutes}:${String(secs).padStart(2, "0")}`;
}

/** Parse `0:44` or `1:02:33` back into seconds. Null when it is not a time. */
export function parseTime(value: string): number | null {
  const match = /^(\d{1,2}):(\d{2})(?::(\d{2}))?$/.exec(value.trim());
  if (!match) return null;
  if (match[3] !== undefined) {
    return Number(match[1]) * 3600 + Number(match[2]) * 60 + Number(match[3]);
  }
  return Number(match[1]) * 60 + Number(match[2]);
}

/**
 * A URL fragment for a heading.
 *
 * Letters of any script are kept, so a Devanagari or Gujarati heading
 * still yields a readable and stable id instead of collapsing to
 * "section".
 */
export function slugify(text: string): string {
  const base = text
    .toLowerCase()
    .replace(/[^\p{Letter}\p{Number}]+/gu, "-")
    .replace(/^-+|-+$/g, "");
  return base || "section";
}

/** The text of a React node tree, with no markup. */
export function nodeText(node: ReactNode): string {
  if (node === null || node === undefined || node === false) return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(nodeText).join("");
  if (typeof node === "object" && "props" in node) {
    const props = (node as { props: { children?: ReactNode } }).props;
    return nodeText(props.children);
  }
  return "";
}

/** Human labels for the pipeline's status values. */
export const STATUS_LABELS: Record<string, string> = {
  pending: "Waiting to start",
  processing: "Processing",
  transcribing: "Transcribing",
  notes: "Writing notes",
  ready: "Ready",
  failed: "Failed",
};
