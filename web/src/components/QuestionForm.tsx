/**
 * The typed question.
 *
 * The text path uses the same retrieval and the same grounded answer as
 * the voice tutor, so a student can ask when the microphone is not
 * available and the answer is the same either way.
 */
import { ErrorState } from "./States";

export function QuestionForm({
  value,
  onChange,
  onSubmit,
  pending,
  error,
  onRetry,
  lastAnswerMs,
}: {
  value: string;
  onChange: (value: string) => void;
  onSubmit: () => void;
  pending: boolean;
  error: Error | null;
  onRetry: () => void;
  lastAnswerMs: number | null;
}) {
  return (
    <section aria-labelledby="ask-heading" className="mt-5">
      <h2
        id="ask-heading"
        className="text-sm font-semibold uppercase tracking-wide text-muted"
      >
        Ask by text
      </h2>
      <form
        className="mt-2"
        onSubmit={(event) => {
          event.preventDefault();
          onSubmit();
        }}
      >
        <label htmlFor="question" className="text-sm font-medium text-ink">
          Your question about this lecture
        </label>
        <textarea
          id="question"
          value={value}
          onChange={(event) => onChange(event.target.value)}
          rows={3}
          maxLength={2000}
          placeholder="What did the teacher say about ..."
          aria-describedby="question-help"
          className="mt-1 w-full rounded-md border border-strong-line bg-white px-3 py-2 text-sm text-ink"
        />
        <p id="question-help" className="mt-1 text-xs text-muted">
          The answer is taken only from this lecture. If the lecture does not
          cover it, the tutor says so.
        </p>
        <div className="mt-2 flex flex-wrap items-center gap-3">
          <button
            type="submit"
            disabled={pending || value.trim().length === 0}
            className="rounded-md bg-accent px-3 py-2 text-sm font-medium text-white hover:bg-accent-ink disabled:opacity-60"
          >
            {pending ? "Thinking…" : "Send"}
          </button>
          {pending ? (
            <span role="status" className="text-sm text-muted">
              Looking for the answer in the lecture.
            </span>
          ) : null}
          {lastAnswerMs !== null && !pending ? (
            <p className="text-xs text-muted">
              Answered in {Math.round(lastAnswerMs)} ms
            </p>
          ) : null}
        </div>
      </form>
      {error ? (
        <div className="mt-3">
          <ErrorState error={error} onRetry={onRetry} />
        </div>
      ) : null}
    </section>
  );
}
