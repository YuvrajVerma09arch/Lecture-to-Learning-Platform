/** The transcript behind a timestamp link. */
import { formatTime } from "../lib/format";
import type { Segment } from "../types";

export function SourceExcerpt({
  start,
  end,
  segments,
}: {
  start: number;
  end: number;
  segments: Segment[];
}) {
  const covering = segments.filter(
    (segment) => segment.end_seconds > start && segment.start_seconds < end,
  );

  return (
    <div
      tabIndex={-1}
      className="mt-2 rounded-md border border-line bg-canvas px-3 py-2"
    >
      <p className="text-xs font-semibold uppercase tracking-wide text-muted">
        Transcript {formatTime(start)} to {formatTime(end)}
      </p>
      {covering.length === 0 ? (
        <p className="mt-1 text-sm text-muted">
          No transcript segment covers this span of the recording.
        </p>
      ) : (
        <ul className="mt-2 space-y-2">
          {covering.map((segment) => (
            <li key={segment.seq} className="text-sm text-body">
              <span className="mr-2 font-mono text-xs text-muted">
                {formatTime(segment.start_seconds)}
              </span>
              {segment.text}
              {segment.visual_dependent ? (
                <span className="mt-1 block border-l-2 border-strong-line pl-2 text-xs text-muted">
                  This part of the recording referred to material shown on
                  screen, so the transcript is incomplete.
                </span>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
