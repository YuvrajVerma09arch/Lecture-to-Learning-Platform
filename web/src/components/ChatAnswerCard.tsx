/** A grounded answer from the text path, with the segments it used. */
import { formatTime } from "../lib/format";
import type { ChatAnswer } from "../types";

export function ChatAnswerCard({
  answer,
  onOpenSource,
}: {
  answer: ChatAnswer;
  onOpenSource: (seconds: number) => void;
}) {
  return (
    <div className="rounded-lg border border-accent bg-white px-3 py-3">
      <p className="text-sm leading-6 text-body">{answer.answer}</p>

      {answer.note ? (
        <p className="mt-2 text-xs text-muted">{answer.note}</p>
      ) : null}

      <p className="mt-2 text-xs text-muted">
        {answer.covered
          ? `Grounded in ${answer.cited_segments.length} segment${
              answer.cited_segments.length === 1 ? "" : "s"
            }.`
          : "Not covered by this lecture."}{" "}
        Confidence {Math.round(answer.confidence * 100)}%.
      </p>

      {answer.retrieved.length > 0 ? (
        <div className="mt-3 border-t border-line pt-2">
          <h3 className="text-xs font-semibold uppercase tracking-wide text-muted">
            Source segments
          </h3>
          <ul className="mt-1 space-y-2">
            {answer.retrieved.map((segment) => (
              <li key={segment.id} className="text-sm text-body">
                <button
                  type="button"
                  onClick={() => onOpenSource(segment.start_seconds)}
                  className="font-mono text-xs text-accent-ink underline underline-offset-2"
                >
                  {formatTime(segment.start_seconds)}
                </button>
                <span className="ml-2">{segment.text}</span>
                {segment.visual_dependent ? (
                  <span className="mt-1 block border-l-2 border-strong-line pl-2 text-xs text-muted">
                    This part of the lecture referred to material shown on
                    screen, so the transcript is incomplete.
                  </span>
                ) : null}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}
