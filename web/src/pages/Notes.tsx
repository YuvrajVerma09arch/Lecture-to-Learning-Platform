/**
 * Screen A: the notes reader.
 *
 * A document column with a heading navigation beside it, timestamp
 * links that open the transcript behind them, and a clear label on the
 * passages that depend on material the recording did not contain.
 */
import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { useAsync } from "../hooks/useAsync";
import { extractHeadings, extractSourceSpans, findSpanForTime, type SourceSpan } from "../lib/markdown";
import { formatTime } from "../lib/format";
import type { Route } from "../lib/router";
import type { Lecture } from "../types";
import { NotesDocument } from "../components/NotesDocument";
import { TableOfContents } from "../components/TableOfContents";
import { EmptyState, ErrorState, LoadingState } from "../components/States";

export function Notes({
  lectureId,
  lectures,
  route,
  onNavigate,
}: {
  lectureId: string | null;
  lectures: Lecture[];
  route: Route;
  onNavigate: (path: string, params?: Record<string, string>) => void;
}) {
  const notes = useAsync(
    () => (lectureId ? api.notes(lectureId) : Promise.resolve(null)),
    [lectureId],
    Boolean(lectureId),
  );
  const [openStart, setOpenStart] = useState<number | null>(null);
  const [requestedStart, setRequestedStart] = useState<number | null>(null);

  const lecture = lectures.find((item) => item.id === lectureId) ?? null;
  const markdown = notes.data?.document ?? "";
  const headings = useMemo(
    () => (markdown ? extractHeadings(markdown) : []),
    [markdown],
  );
  const spans = useMemo(
    () => (markdown ? extractSourceSpans(markdown) : []),
    [markdown],
  );

  // A link from the chat screen can ask for a moment in the recording.
  useEffect(() => {
    const requested = route.params.get("source");
    if (requested === null) return;
    const seconds = Number(requested);
    if (Number.isFinite(seconds)) setRequestedStart(seconds);
  }, [route.params]);

  // The document links point at a section, the chat links at a segment,
  // so the section that covers the moment is the one to open.
  const requestedSpan = useMemo(
    () => (requestedStart === null ? null : findSpanForTime(spans, requestedStart)),
    [spans, requestedStart],
  );

  useEffect(() => {
    if (!requestedSpan) return;
    setOpenStart(requestedSpan.start);
    const button = document.querySelector<HTMLElement>(
      `[data-source-start="${requestedSpan.start}"]`,
    );
    button?.scrollIntoView?.({ behavior: "smooth", block: "center" });
  }, [requestedSpan]);

  const activeSpan: SourceSpan | null =
    spans.find((span) => span.start === openStart) ?? null;

  const toggleSource = (span: SourceSpan) => {
    setOpenStart((current) => (current === span.start ? null : span.start));
  };

  if (!lectureId) {
    return (
      <main id="main" className="mx-auto max-w-5xl px-4 py-6">
        <EmptyState
          title="Choose a lecture"
          body="Pick a lecture above to read its notes. Each lecture is a recording that has already been transcribed and turned into study notes."
        />
      </main>
    );
  }

  if (notes.status === "loading") {
    return (
      <main id="main" className="mx-auto max-w-5xl px-4 py-6">
        <LoadingState label="Loading the notes" />
      </main>
    );
  }

  if (notes.status === "error" && notes.error) {
    return (
      <main id="main" className="mx-auto max-w-5xl px-4 py-6">
        <ErrorState error={notes.error} onRetry={notes.reload} />
      </main>
    );
  }

  const payload = notes.data;
  if (!payload || !payload.document) {
    const failed = lecture?.status === "failed";
    return (
      <main id="main" className="mx-auto max-w-5xl px-4 py-6">
        <EmptyState
          title={failed ? "This lecture did not finish" : "The notes are not ready yet"}
          body={
            failed
              ? `The recording was processed up to the ${
                  lecture?.failed_stage ?? "unknown"
                } stage, which failed. ${
                  lecture?.failure_reason ?? ""
                }`.trim()
              : `This lecture is ${
                  payload?.status ?? "not finished"
                }. The notes appear here as soon as the notes pass finishes.`
          }
        />
      </main>
    );
  }

  return (
    <main id="main" className="mx-auto max-w-5xl px-4 pb-16 pt-4">
      <div className="flex flex-col gap-4 lg:grid lg:grid-cols-[minmax(0,1fr)_17rem] lg:gap-10">
        <div className="order-2 min-w-0 lg:order-1">
          <p className="text-sm text-muted">
            {lecture ? `${formatTime(lecture.audio_seconds)} recording` : null}
            {lecture && lecture.audio_seconds ? " · " : ""}
            {payload.segments.length} transcript segments
          </p>
          <div className="mt-2 rounded-lg border border-line bg-white px-4 py-5 sm:px-6">
            <NotesDocument
              markdown={payload.document}
              segments={payload.segments}
              activeSpan={activeSpan}
              onToggleSource={toggleSource}
            />
          </div>
        </div>

        <div className="order-1 lg:order-2">
          <div className="sticky top-16 z-20 lg:top-20">
            <TableOfContents headings={headings} />
            <button
              type="button"
              onClick={() =>
                onNavigate("/chat", {
                  lecture: lectureId,
                })
              }
              className="mt-3 w-full rounded-md border border-strong-line bg-white px-3 py-2 text-sm font-medium text-ink hover:bg-canvas"
            >
              Ask a doubt about this lecture
            </button>
          </div>
        </div>
      </div>
    </main>
  );
}
